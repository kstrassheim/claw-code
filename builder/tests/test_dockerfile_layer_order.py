"""Expensive layers first, volatile ones last — checked, not remembered.

Docker invalidates every layer after the first one that changes. The image
therefore has a shape: base packages, then tool installs pinned by ARG, then
LAST the scripts and modules from this repository, which change on almost
every commit.

Get that order wrong and nothing breaks — it just costs twenty minutes a
build, silently, forever. It happened the day mermaid-cli was added: the block
went in beside the chromium layer, which is above the security venvs
(semgrep, bandit, pip-audit), the .NET SDK and nuclei, so a mermaid version
bump rebuilt all of them. The build still worked. Only the clock noticed.

So the rule is mechanical:

    no ARG <TOOL>_VERSION, and no package install, may appear AFTER the
    first COPY of a file that changes with ordinary work.

That COPY is the boundary — everything below it is cheap and expected to
rebuild; everything above it should survive a source-only change.
"""

import os
import re
import unittest

from harness import BUILDER

DOCKERFILE = os.path.join(BUILDER, "Dockerfile")

# The first thing COPYed that changes with ordinary work. Everything from here
# down is the volatile tail of the image.
VOLATILE_BOUNDARY = re.compile(r"^COPY\s+--chmod=0755\s+heartbeat-issue-tick\.py")

VERSION_ARG = re.compile(r"^ARG\s+([A-Z0-9_]*VERSION)\b")
INSTALL = re.compile(
    r"^RUN\b.*?(apt-get install|npm install|pip install|python3 -m venv"
    r"|dotnet-install|curl -fsSL -o /usr/local/bin)", re.S)


def lines():
    with open(DOCKERFILE, encoding="utf-8") as fh:
        return fh.read().splitlines()


def boundary_index(src):
    for n, line in enumerate(src):
        if VOLATILE_BOUNDARY.match(line):
            return n
    raise AssertionError("the volatile boundary COPY is gone — update this test")


class NothingExpensiveLivesBelowTheVolatileBoundary(unittest.TestCase):
    def setUp(self):
        self.src = lines()
        self.boundary = boundary_index(self.src)

    def test_no_version_arg_after_the_boundary(self):
        # An ARG whose value moves invalidates its layer and everything under
        # it. Below the boundary that is fine; above it, a version bump would
        # rebuild the repo scripts too — which is cheap. The expensive
        # direction is a version ARG placed too HIGH, caught by the next test.
        late = [(n + 1, l) for n, l in enumerate(self.src)
                if n > self.boundary and VERSION_ARG.match(l)]
        self.assertEqual(late, [], f"version ARGs below the script COPYs: {late}")

    def test_no_package_install_after_the_boundary(self):
        late = [(n + 1, l[:60]) for n, l in enumerate(self.src)
                if n > self.boundary and INSTALL.match(l)]
        self.assertEqual(late, [], f"installs below the script COPYs: {late}")


class TheSlowLayersStayAboveTheFastOnes(unittest.TestCase):
    """The specific regression: a cheap install placed above expensive ones."""

    def setUp(self):
        self.src = lines()

    def line_of(self, needle):
        for n, l in enumerate(self.src):
            if needle in l:
                return n
        raise AssertionError(f"not found in the Dockerfile: {needle}")

    def test_mermaid_installs_after_the_security_venvs(self):
        # semgrep and friends are minutes; mermaid-cli is seconds. The cheap
        # one must not sit above the expensive one.
        self.assertGreater(self.line_of("@mermaid-js/mermaid-cli@"),
                           self.line_of("python3 -m venv /opt/security-venv"))

    def test_mermaid_installs_after_the_dotnet_sdk(self):
        self.assertGreater(self.line_of("@mermaid-js/mermaid-cli@"),
                           self.line_of("dotnet-install.sh"))

    def test_mermaid_still_installs_after_chromium(self):
        # It reuses the system browser, so it genuinely depends on that layer —
        # the one ordering constraint it really has.
        self.assertGreater(self.line_of("@mermaid-js/mermaid-cli@"),
                           self.line_of("chromium fonts-liberation"))


class EveryVersionArgIsPinnedInVersions(unittest.TestCase):
    """A cache key that is not in VERSIONS is a rebuild nobody asked for."""

    def test_each_version_arg_has_a_pin(self):
        with open(os.path.join(os.path.dirname(BUILDER), "VERSIONS"),
                  encoding="utf-8") as fh:
            versions = fh.read()
        missing = []
        for line in lines():
            m = VERSION_ARG.match(line)
            # BASE_IMAGE and the upstream ref are supplied by the workflow.
            if m and m.group(1) not in ("OPENCLAW_VERSION",):
                if f"{m.group(1)}=" not in versions:
                    missing.append(m.group(1))
        self.assertEqual(missing, [],
                         f"ARGs with no pin in VERSIONS: {missing}")


# Observed, not guessed: 96 instructions build, 101 do not. The image is a
# single stage, so its instruction layers stack on top of the base image's
# and overlayfs refuses at 128. Set at the count that is known to work.
MAX_LAYERS = 96

LAYER = re.compile(r"^(RUN|COPY|ADD)\b")


class TheLayerBudget(unittest.TestCase):
    """Overlayfs stops at 128, and this image is one stage.

    Adding five COPY lines took it from 96 instructions to 101 and the build
    died in CI with `max depth exceeded` — after five minutes, during export,
    with nothing in the message naming the Dockerfile. Nothing about that
    error tells you the fix is to merge COPY lines.

    Files that keep their own name can share one COPY with a directory
    destination; only a rename needs a line of its own. That is why several
    COPYs here list many sources.
    """

    def test_the_image_stays_under_the_depth_limit(self):
        count = sum(1 for l in lines() if LAYER.match(l))
        self.assertLessEqual(count, MAX_LAYERS, (
            f"{count} layer-producing instructions, budget is {MAX_LAYERS}. "
            "Overlayfs refuses beyond 128 counting the base image's own "
            "layers, and the build fails late with 'max depth exceeded'. "
            "Merge COPY lines that share a mode and a destination directory "
            "— `COPY --chmod=0755 a b c /usr/local/bin/` — rather than "
            "raising this number."))


if __name__ == "__main__":
    unittest.main()

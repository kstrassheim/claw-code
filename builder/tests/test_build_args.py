"""Every pin the image needs has to REACH the image.

WHAT THIS CATCHES, AND WHY A REVIEW DID NOT
--------------------------------------------
A version pin travels three hops: `VERSIONS` -> the job environment ->
`--build-arg` -> the `ARG` the Dockerfile reads. Miss the third hop and the
ARG is simply EMPTY. Nothing warns: the layer builds a URL with a hole in it
(`.../download/v/tea--linux-arm64`) and the registry answers 404 — but only
when that layer actually rebuilds. Until then the cached layer keeps serving
the binary from the last build that did pass the argument.

That is exactly how it got in. A pin was added to VERSIONS and echoed into
$GITHUB_ENV for both workflows, and only one of them was given the matching
--build-arg. Two pull requests went green on the cached layer. The third
touched VERSIONS, invalidated the cache, and the image build failed on a
404 for a URL whose version had been verified by hand as reachable.

So the check is not "is the version right" — it is "does the value get
there at all", which no amount of reading the URL can tell you.
"""

import os
import re
import unittest

BUILDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(BUILDER)
DOCKERFILE = os.path.join(BUILDER, "Dockerfile")
WORKFLOWS = [os.path.join(ROOT, ".github", "workflows", f)
             for f in ("validate.yml", "deploy.yml")]

# Computed by the build rather than pinned in VERSIONS: BASE_IMAGE is the
# mirrored upstream reference, EPHE_FETCH_NONCE is a cache-buster carrying the
# image tag, TARGETARCH is buildkit's own, VSCODE_VERSION is passed elsewhere.
NOT_FROM_VERSIONS = {"BASE_IMAGE", "EPHE_FETCH_NONCE", "TARGETARCH",
                     "VSCODE_VERSION", "BUILDKIT_INLINE_CACHE"}


def declared_args() -> set[str]:
    """Every ARG the Dockerfile reads."""
    with open(DOCKERFILE, encoding="utf-8") as fh:
        return set(re.findall(r"^ARG ([A-Z_][A-Z0-9_]*)\s*$", fh.read(), re.M))


def passed_args(path: str) -> set[str]:
    """Every ARG a workflow hands to `docker build`.

    Both spellings occur: the flag inline, and the array of "NAME=${NAME}"
    strings that one workflow expands into flags.
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    inline = set(re.findall(r'--build-arg "([A-Z_][A-Z0-9_]*)=', text))
    # The array form's value is not always a bare ${NAME}: BASE_IMAGE carries a
    # literal prefix. Match the NAME and ignore whatever it is set to.
    array = set(re.findall(r'^\s+"([A-Z_][A-Z0-9_]*)=[^"]*"', text, re.M))
    # And the third spelling: docker/build-push-action's `build-args:` block,
    # one unquoted NAME=value per line. A workflow using it satisfies the same
    # invariant, and a check that only recognised the other two would report a
    # build passing every argument as passing none.
    block = set()
    for m in re.finditer(r'^(\s+)build-args: \|\n((?:\1  .*\n)+)', text, re.M):
        block |= set(re.findall(r'^\s*([A-Z_][A-Z0-9_]*)=', m.group(2), re.M))
    return inline | array | block


def pinned() -> set[str]:
    with open(os.path.join(ROOT, "VERSIONS"), encoding="utf-8") as fh:
        return set(re.findall(r"^([A-Z_][A-Z0-9_]*)=", fh.read(), re.M))


class EveryArgReachesTheBuild(unittest.TestCase):
    maxDiff = None

    def test_both_workflows_pass_every_arg_the_dockerfile_reads(self):
        needed = declared_args() - NOT_FROM_VERSIONS
        for path in WORKFLOWS:
            with self.subTest(workflow=os.path.basename(path)):
                missing = sorted(needed - passed_args(path))
                self.assertEqual(missing, [], (
                    f"{os.path.basename(path)} never passes these as "
                    "--build-arg, so the ARG is empty inside the image. The "
                    "layer will build a URL with a hole in it and 404 — but "
                    "only once the cache is invalidated, which is why this "
                    "survives review."))

    def test_a_pin_that_is_passed_is_a_pin_that_exists(self):
        # The other direction: a --build-arg for a name VERSIONS no longer
        # defines expands to nothing, which fails the same silent way.
        known = pinned() | NOT_FROM_VERSIONS
        for path in WORKFLOWS:
            with self.subTest(workflow=os.path.basename(path)):
                unknown = sorted(a for a in passed_args(path) if a not in known)
                self.assertEqual(unknown, [], (
                    "these are passed to docker build but are not defined in "
                    "VERSIONS, so they expand to an empty string"))

    def test_the_two_workflows_agree(self):
        # They build the same image. A pin one passes and the other does not
        # means the pull request and the deploy build different things.
        #
        # Compared over PINS only: the build-system arguments legitimately
        # differ (only the deploy exports an inline cache), and folding those
        # in would make this fail for a reason that is not a drifting pin.
        a, b = (passed_args(p) - NOT_FROM_VERSIONS for p in WORKFLOWS)
        self.assertEqual(sorted(a - b), [], "validate.yml passes what deploy.yml does not")
        self.assertEqual(sorted(b - a), [], "deploy.yml passes what validate.yml does not")


if __name__ == "__main__":
    unittest.main()

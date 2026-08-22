"""The runners must source their libraries, never their CLIs.

Each runtime knob ships as a PAIR in the same directory: `agent-limits` is the
CLI a human runs, `agent-limits.sh` is the library the runners source.
Searching the bare name first finds the CLI — and sourcing a CLI does not
merely fail to define the helpers. The CLI parses its absent subcommand and
calls `exit`, which in a sourced file belongs to the CALLER, so the runner
terminates before doing any work.

The symptom carried no error anywhere: the spawner reported a successful spawn
every five minutes while no run ever started, no lock was taken and no log was
written. Nothing said the word "fail".

These tests run the REAL `_source_lib` lifted out of each shipped runner, so
they fail if someone reorders the search back.

A note for anyone tempted to guard on the execute bit instead of the suffix:
the sandbox this repository is developed in forces mode 0755 on every file, so
`chmod 0644` is a no-op there and a mode-based guard rejects the real library.
The Dockerfile assertion below covers the packaging side instead.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from harness import BUILDER, TMP_ROOT

RUNNERS = ("fixer-runner.sh", "reviewer-runner.sh", "tester-runner.sh")


def source_lib_of(runner: str) -> str:
    """The real _source_lib function, lifted verbatim from the runner."""
    path = os.path.join(BUILDER, runner)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^_source_lib\(\) \{.*?^\}", text, re.S | re.M)
    assert m, f"_source_lib not found in {runner} — if it was renamed, update this test"
    return m.group(0)


class SourceLibPrefersTheLibrary(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="srclib-", dir=TMP_ROOT)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text, mode):
        p = os.path.join(self.bin, name)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(p, mode)
        return p

    def pair(self):
        """A CLI and a library with the same stem, as the image installs them.

        The CLI exits the way the real one does, so a test that sources it
        fails the way production did rather than merely picking a wrong file.
        """
        self.write("agent-limits",
                   "#!/bin/sh\necho 'unknown command: agent-limits' >&2\nexit 2\n", 0o755)
        self.write("agent-limits.sh",
                   "agent_limit() { echo SOURCED_THE_LIBRARY; }\n", 0o644)

    def run_fn(self, runner, script):
        body = "\n".join([
            "set -u",
            f'LIB_DIR="{self.bin}"',
            source_lib_of(runner),
            script,
        ])
        path = os.path.join(self.dir, "drive.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        p = subprocess.run(["bash", path], capture_output=True, text=True,
                           cwd=self.dir, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_every_runner_sources_the_library_not_the_cli(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner):
                self.pair()
                rc, out, err = self.run_fn(
                    runner,
                    "_source_lib agent-limits || echo NOT_FOUND\n"
                    "agent_limit x 2>/dev/null || echo HELPER_MISSING\n"
                    "echo REACHED_THE_END\n")
                self.assertNotIn("unknown command", err,
                                 f"{runner} sourced the CLI instead of the library")
                self.assertIn("SOURCED_THE_LIBRARY", out, f"{runner}: helper undefined")
                self.assertNotIn("NOT_FOUND", out, f"{runner}: library not found")
                self.assertIn("REACHED_THE_END", out,
                              f"{runner}: the caller died while sourcing")

    def test_sourcing_the_cli_would_kill_the_caller(self):
        # Pins WHY this matters: the guard protects against a silent abort,
        # not merely an undefined function.
        self.pair()
        path = os.path.join(self.dir, "victim.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f'. "{self.bin}/agent-limits" 2>/dev/null\necho REACHED_THE_END\n')
        p = subprocess.run(["bash", path], capture_output=True, text=True, timeout=60)
        self.assertNotIn("REACHED_THE_END", p.stdout,
                         "sourcing the CLI must abort the caller — if that ever "
                         "stops being true, this guard can be relaxed")

    def test_a_bare_named_file_is_never_sourced(self):
        # The suffix is the whole rule. A directory holding ONLY the CLI must
        # yield nothing rather than sourcing it — this is the exact production
        # shape, since /usr/local/bin holds both and the CLI sorts first by
        # every other search order.
        for runner in RUNNERS:
            with self.subTest(runner=runner):
                self.write("solo", "echo SHOULD_NOT_BE_SOURCED\n", 0o755)
                rc, out, err = self.run_fn(
                    runner, "_source_lib solo || echo NOT_FOUND\n")
                self.assertIn("NOT_FOUND", out)
                self.assertNotIn("SHOULD_NOT_BE_SOURCED", out)

    def test_the_library_is_shipped_non_executable(self):
        # The invariant above only holds if the Dockerfile keeps installing the
        # libraries 0644. This is the line that would silently break it.
        with open(os.path.join(BUILDER, "Dockerfile"), encoding="utf-8") as f:
            dockerfile = f.read()
        for lib in ("agent-limits.sh", "agent-models.sh",
                    "agent-thinking.sh", "agent-slot.sh"):
            with self.subTest(lib=lib):
                m = re.search(rf"COPY --chmod=(\d+) {re.escape(lib)}\s", dockerfile)
                self.assertIsNotNone(m, f"{lib} is not installed by the Dockerfile")
                self.assertEqual(m.group(1), "0644",
                                 f"{lib} must ship non-executable, or a runner "
                                 f"may source it as a CLI")


if __name__ == "__main__":
    unittest.main()

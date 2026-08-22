"""What the solver believes about CI, and where that belief comes from.

WHY THIS FILE EXISTS NOW
------------------------
These helpers decide whether a change may be reviewed and merged, and none of
them had a test. That was survivable only while each carried its own reading
of a host's raw payload — there was nothing to test but a JSON walk. They now
ask one question and translate the answer, so the translation is the thing
worth pinning, and it is small enough to pin exactly.

The reading they used to do was not even consistent. Four helpers each fetched
the head commit for themselves; one hashed raw conclusions; the tester's copy
called a pass `success` where every other copy said `green`. Any of those
drifting is a gate that opens when it should not, and the log would look
completely ordinary while it happened.

THE PROPERTY THAT MATTERS MOST is that a commit nobody ran anything on does
NOT read as green. `none` and `green` are one keystroke apart in a case
statement and opposite in consequence: one waits for CI, the other merges work
that nothing has tested.
"""

import os
import unittest

from harness import BUILDER, ShellTestCase, fake_path

RUNNER = "fixer-runner.sh"
START = "# The head commit of a change, asked once and reused"
END = "# List requested-reviewer logins"


class CiHelpers(ShellTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures = os.path.join(self.home, "fx")
        os.makedirs(self.fixtures, exist_ok=True)
        import shutil
        src = self.extract_block(RUNNER, START, END)
        shutil.move(src, os.path.join(self.home, "ci.sh"))

    def fixture(self, slug, payload):
        import json
        with open(os.path.join(self.fixtures, slug), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(payload if isinstance(payload, str) else json.dumps(payload))

    def run_helper(self, call, expect_ok=True):
        script = "\n".join([
            "set -u",
            'REPO=o/r',
            'FORGE=(forge-cli --repo "$REPO")',
            f'export FAKE_FORGE_DIR="{self.fixtures}"',
            f'export PATH="{fake_path(os.path.join(self.home, "bin"))}:$PATH"',
            '. "$PWD/ci.sh"',
            call,
        ])
        rc, out, err = self.sh(script)
        if expect_ok:
            self.assertEqual(rc, 0, err or out)
        return out.strip()

    # -- the head commit ---------------------------------------------------

    def test_the_head_commit_of_a_change_is_read_once(self):
        self.fixture("change-request_7", {"number": 7, "headSha": "abc123"})
        self.assertEqual(self.run_helper('head_sha_of_pr 7'), "abc123")

    def test_an_unreadable_change_has_no_head_rather_than_a_wrong_one(self):
        # Every helper below gates on this being EMPTY, not on its exit code —
        # a stand-in value would send them asking about a commit that does not
        # exist. The status may be non-zero and is not what any caller reads.
        self.assertEqual(
            self.run_helper('head_sha_of_pr 9', expect_ok=False), "")

    # -- the gate ----------------------------------------------------------

    def test_green_checks_open_the_gate(self):
        self.fixture("change-request_7", {"number": 7, "headSha": "abc"})
        self.fixture("checks_abc", "green")
        self.assertEqual(self.run_helper('ci_status_for_pr 7'), "green")

    def test_a_commit_nobody_tested_does_not_read_as_green(self):
        # THE assertion of this file. `none` means no check ever ran on this
        # commit — it says nothing about whether the change works, and reading
        # it as a pass merges work nothing has tested.
        self.fixture("change-request_7", {"number": 7, "headSha": "abc"})
        self.fixture("checks_abc", "none")
        self.assertEqual(self.run_helper('ci_status_for_pr 7'), "pending")

    def test_checks_still_running_are_not_a_failure(self):
        # Distinct from failed: the solver waits on pending and goes off to
        # fix on not_green, and confusing them makes it "fix" a passing build.
        self.fixture("change-request_7", {"number": 7, "headSha": "abc"})
        self.fixture("checks_abc", "pending")
        self.assertEqual(self.run_helper('ci_status_for_pr 7'), "pending")

    def test_a_failing_check_closes_the_gate(self):
        self.fixture("change-request_7", {"number": 7, "headSha": "abc"})
        self.fixture("checks_abc", "failed")
        self.assertEqual(self.run_helper('ci_status_for_pr 7'), "not_green")

    def test_a_change_with_no_head_is_unknown_not_green(self):
        self.assertEqual(self.run_helper('ci_status_for_pr 9'), "unknown")

    # -- the fingerprint ---------------------------------------------------

    def test_a_commit_with_no_checks_yet_is_marked_as_such(self):
        self.fixture("change-request_7", {"number": 7, "headSha": "abcdef01234"})
        self.fixture("check-list_abcdef01234", [])
        self.assertEqual(self.run_helper('ci_fingerprint_for_pr 7'),
                         "no-checks:abcdef0")

    def test_a_run_still_going_is_in_progress(self):
        self.fixture("change-request_7", {"number": 7, "headSha": "abcdef01234"})
        self.fixture("check-list_abcdef01234",
                     [{"name": "build", "state": "green"},
                      {"name": "test", "state": "pending"}])
        self.assertEqual(self.run_helper('ci_fingerprint_for_pr 7'),
                         "in-progress:abcdef0")

    def test_the_same_settled_checks_produce_the_same_fingerprint(self):
        # The fingerprint is what decides whether to wake the agent. If it
        # changed on its own the solver would burn a model call every tick.
        self.fixture("change-request_7", {"number": 7, "headSha": "abcdef01234"})
        self.fixture("check-list_abcdef01234",
                     [{"name": "build", "state": "green"},
                      {"name": "test", "state": "failed"}])
        first = self.run_helper('ci_fingerprint_for_pr 7')
        self.assertEqual(first, self.run_helper('ci_fingerprint_for_pr 7'))
        self.assertTrue(first.startswith("settled:abcdef0:"))

    def test_a_different_outcome_changes_the_fingerprint(self):
        self.fixture("change-request_7", {"number": 7, "headSha": "abcdef01234"})
        self.fixture("check-list_abcdef01234",
                     [{"name": "build", "state": "green"},
                      {"name": "test", "state": "failed"}])
        red = self.run_helper('ci_fingerprint_for_pr 7')
        self.fixture("check-list_abcdef01234",
                     [{"name": "build", "state": "green"},
                      {"name": "test", "state": "green"}])
        self.assertNotEqual(red, self.run_helper('ci_fingerprint_for_pr 7'))

    def test_the_head_commit_is_part_of_the_fingerprint(self):
        # A push whose checks settle with exactly the same conclusions as the
        # previous commit is still a new thing to look at — a fix that did not
        # fix would otherwise be indistinguishable from no change at all.
        self.fixture("change-request_7", {"number": 7, "headSha": "aaaaaaa1111"})
        self.fixture("check-list_aaaaaaa1111", [{"name": "test", "state": "failed"}])
        first = self.run_helper('ci_fingerprint_for_pr 7')
        self.fixture("change-request_7", {"number": 7, "headSha": "bbbbbbb2222"})
        self.fixture("check-list_bbbbbbb2222", [{"name": "test", "state": "failed"}])
        self.assertNotEqual(first, self.run_helper('ci_fingerprint_for_pr 7'))

    # -- the summary a person and the agent read ---------------------------

    def test_the_summary_names_every_check_and_its_state(self):
        self.fixture("change-request_7", {"number": 7, "headSha": "abc"})
        self.fixture("check-list_abc", [{"name": "build", "state": "green"},
                                        {"name": "test", "state": "failed"}])
        out = self.run_helper('ci_summary_text_for_pr 7')
        self.assertIn("build", out)
        self.assertIn("green", out)
        self.assertIn("test", out)
        self.assertIn("failed", out)

    def test_a_commit_with_no_checks_says_so_rather_than_nothing(self):
        # Empty output would reach the agent's prompt as a blank section,
        # which reads as "CI passed" to a model skimming for problems.
        self.fixture("change-request_7", {"number": 7, "headSha": "abc"})
        self.fixture("check-list_abc", [])
        self.assertIn("no checks", self.run_helper('ci_summary_text_for_pr 7'))


if __name__ == "__main__":
    unittest.main()

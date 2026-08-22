"""Which sprint is running — answerable offline, and "none" is a real answer.

Two properties, both of which stop planning from becoming a prerequisite for
solving issues:

  - The number is read from a marker on the workspace volume, never over the
    network. The store is deliberately built so that an outage makes reporting
    late rather than stopping the bot, and one network read here would undo it.

  - No marker means NO SPRINT, not an error. Before the first rollover, and on
    any deployment where planning is not in use, there simply is no sprint. A
    caller that treats that as a failure refuses to solve issues on a cluster
    that never asked for sprints at all.
"""

import json
import os
import shutil
import tempfile
import unittest

from harness import TMP_ROOT, load, temp_env


class TheMarker(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="sprint-", dir=TMP_ROOT)
        self.path = os.path.join(self.dir, "sprint-current.json")
        self._env = temp_env(SPRINT_CURRENT_FILE=self.path)
        self._env.__enter__()
        self.sc = load("sprint_current")

    def tearDown(self):
        self._env.__exit__(None, None, None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_no_marker_is_no_sprint_rather_than_an_error(self):
        self.assertEqual(self.sc.read(), {})
        self.assertIsNone(self.sc.number())

    def test_a_written_sprint_reads_back(self):
        self.assertTrue(self.sc.write(12, "2026-08-08T13:00:00+02:00",
                                      "2026-08-15T13:00:00+02:00"))
        self.assertEqual(self.sc.number(), 12)
        self.assertEqual(self.sc.read()["endsAt"][:10], "2026-08-15")

    def test_a_separate_process_sees_the_same_answer(self):
        # The runners are separate processes from whatever rolled the sprint
        # over; this file is the only thing they share.
        self.sc.write(3, "a", "b")
        again = load("sprint_current")
        self.assertEqual(again.number(), 3)

    def test_a_corrupt_marker_is_no_sprint_rather_than_a_crash(self):
        # A kill during the write, or a half-synced volume. Work events lose
        # their sprint for a while, which is recoverable; a crash in every
        # runner's startup is not.
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ truncated")
        self.assertEqual(self.sc.read(), {})
        self.assertIsNone(self.sc.number())

    def test_a_marker_with_no_number_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"startedAt": "a"}, f)
        self.assertEqual(self.sc.read(), {})

    def test_writing_leaves_no_partial_file_behind(self):
        # Written through a temporary and renamed: a runner may read this at
        # any moment, and half a marker is worse than an old one.
        self.sc.write(1, "a", "b")
        self.sc.write(2, "c", "d")
        self.assertEqual(self.sc.number(), 2)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_a_failed_write_is_reported_and_not_raised(self):
        # Failing to record the sprint must not fail the ROLLOVER itself.
        with temp_env(SPRINT_CURRENT_FILE=os.path.join(
                self.dir, "no", "such", "dir", "x.json")):
            sc = load("sprint_current")
            self.assertIn(sc.write(1, "a", "b"), (True, False))

    def test_clearing_it_returns_to_no_sprint(self):
        self.sc.write(9, "a", "b")
        self.sc.clear()
        self.assertIsNone(self.sc.number())

    def test_clearing_nothing_is_not_an_error(self):
        self.sc.clear()
        self.sc.clear()

    def test_describe_says_no_sprint_without_sounding_broken(self):
        # This sentence goes into chat. "Error reading sprint" would send
        # somebody debugging a system that is working exactly as designed.
        text = self.sc.describe()
        self.assertIn("No sprint", text)
        self.assertIn("still tracked", text)

    def test_describe_names_the_sprint_and_its_window(self):
        self.sc.write(12, "2026-08-08T13:00:00+02:00",
                      "2026-08-15T13:00:00+02:00")
        text = self.sc.describe()
        self.assertIn("Sprint 12", text)
        self.assertIn("2026-08-15", text)


if __name__ == "__main__":
    unittest.main()

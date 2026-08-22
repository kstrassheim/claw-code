"""Recording what a run cost, from a shell runner, without ever failing it.

Two things are being defended here.

THE COUNT COMES FROM THE LOG, NOT FROM A COUNTER. A counter kept by the runner
would be a second source of truth and would drift from the thing the
story-point scale was calibrated against. The offset is the other half of that:
runner logs accumulate across runs, so counting the whole file would charge
every previous run's calls to this one, and the resulting history row would
tell the estimator the exact opposite of the truth.

IT CANNOT EXIT NON-ZERO. This is called from a runner's exit path. Every way
of getting it wrong — no store, no issue number, no log — must still leave the
run that produced the work reported as successful.
"""

import json
import os
import shutil
import tempfile
import unittest

from harness import TMP_ROOT, load, load_script, temp_env


class CountingModelCalls(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="record-", dir=TMP_ROOT)
        self.log = os.path.join(self.dir, "runner.log")
        self.rec = load_script("planning-record")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_log(self, *lines):
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def test_model_fetch_lines_are_what_gets_counted(self):
        self.write_log(
            "starting up",
            "[model-fetch] provider=minimax status=200 tokens=1200",
            "some unrelated chatter",
            "[model-fetch] provider=minimax status=200 tokens=900",
        )
        calls, provider, rate = self.rec.count_calls(self.log, 0)
        self.assertEqual(calls, 2)
        self.assertEqual(provider, "minimax")
        self.assertEqual(rate, 0)

    def test_lines_before_the_offset_belong_to_a_previous_run(self):
        # The log is per issue and accumulates. Without the offset this run is
        # charged with everything the issue ever cost.
        self.write_log(
            "[model-fetch] provider=minimax status=200",
            "[model-fetch] provider=minimax status=200",
            "--- this run starts here ---",
            "[model-fetch] provider=minimax status=200",
        )
        self.assertEqual(self.rec.count_calls(self.log, 3)[0], 1)

    def test_rate_limited_calls_are_counted_and_still_count_as_calls(self):
        # A 429 is a call that was made and paid for in wall-clock time. It is
        # reported separately AND included, because dropping it would make a
        # throttled run look cheap.
        self.write_log(
            "[model-fetch] provider=minimax status=429",
            "[model-fetch] provider=minimax status=200",
        )
        calls, _, rate = self.rec.count_calls(self.log, 0)
        self.assertEqual((calls, rate), (2, 1))

    def test_a_line_that_merely_mentions_a_status_is_not_a_call(self):
        self.write_log("retrying after status=500 from the API",
                       "[model-fetch] queued")
        self.assertEqual(self.rec.count_calls(self.log, 0)[0], 0)

    def test_a_missing_log_is_zero_calls_rather_than_a_failure(self):
        # The run itself succeeded. Refusing to record anything because the
        # log moved would lose the outcome as well as the cost.
        calls, provider, rate = self.rec.count_calls(
            os.path.join(self.dir, "nope.log"), 0)
        self.assertEqual((calls, provider, rate), (0, "", 0))


class RecordingARun(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="record-", dir=TMP_ROOT)
        self.spool = os.path.join(self.dir, "planning-spool.jsonl")
        self.log = os.path.join(self.dir, "runner.log")
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("[model-fetch] provider=minimax status=200\n" * 7)
        env = temp_env(PLANNING_SPOOL=self.spool,
                       PLANNING_MONGO_URI=None,
                       SPRINT_CURRENT_FILE=os.path.join(self.dir, "sprint.json"))
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        # Prime the modules the command imports so they read THIS test's
        # environment rather than a copy left behind by an earlier test.
        load("planning_store")
        load("sprint_current")
        self.rec = load_script("planning-record")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def spooled(self):
        if not os.path.exists(self.spool):
            return []
        with open(self.spool, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def run_record(self, *argv):
        return self.rec.main(list(argv))

    def test_a_solver_run_is_recorded_against_its_story(self):
        rc = self.run_record("--role", "solver", "--repo", "acme/web",
                             "--issue", "42", "--run-id", "run-1",
                             "--log", self.log, "--outcome", "merged")
        self.assertEqual(rc, 0)
        docs = self.spooled()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["pk"], "story#github#acme/web#42")
        self.assertEqual(docs[0]["llmCalls"], 7)
        self.assertEqual(docs[0]["outcome"], "merged")

    def test_recording_the_same_run_twice_keeps_one_id(self):
        # --follow calls this on a timer. Each write must replace the last, or
        # a long solve appears in the reports as many separate runs.
        for _ in range(3):
            self.run_record("--role", "solver", "--repo", "acme/web",
                            "--issue", "42", "--run-id", "run-1",
                            "--log", self.log)
        ids = {d["id"] for d in self.spooled()}
        self.assertEqual(ids, {"work#run-1#solver"})

    def test_the_tester_records_against_the_commit_it_tested(self):
        rc = self.run_record("--role", "tester", "--repo", "acme/web",
                             "--sha", "abc123def4567", "--run-id", "t-1",
                             "--log", self.log)
        self.assertEqual(rc, 0)
        self.assertEqual(self.spooled()[0]["pk"],
                         "deploy#github#acme/web#abc123def456")

    def test_a_tester_run_with_no_sha_records_nothing_and_still_exits_zero(self):
        self.assertEqual(
            self.run_record("--role", "tester", "--repo", "acme/web",
                            "--run-id", "t-1"), 0)
        self.assertEqual(self.spooled(), [])

    def test_a_story_run_with_no_issue_number_records_nothing_and_exits_zero(self):
        self.assertEqual(
            self.run_record("--role", "solver", "--repo", "acme/web",
                            "--run-id", "r-1"), 0)
        self.assertEqual(self.spooled(), [])

    def test_an_explicit_count_overrides_the_log(self):
        self.run_record("--role", "solver", "--repo", "acme/web",
                        "--issue", "42", "--run-id", "r-1",
                        "--log", self.log, "--calls", "99")
        self.assertEqual(self.spooled()[0]["llmCalls"], 99)

    def test_a_run_with_no_sprint_is_still_recorded(self):
        # Planning must never become a prerequisite for solving issues: before
        # the first rollover there is no sprint, and the work still happened.
        self.run_record("--role", "solver", "--repo", "acme/web",
                        "--issue", "42", "--run-id", "r-1", "--log", self.log)
        self.assertIsNone(self.spooled()[0]["sprintId"])


if __name__ == "__main__":
    unittest.main()

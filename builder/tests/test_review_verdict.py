"""The reviewer's two irreversible acts: posting a verdict, and taking a lock.

WHY THIS IS A SHELL TEST
------------------------
Both decisions live in reviewer-runner.sh, and both are the kind that cannot
be undone by the next tick. A verdict comment is the issue-solver's merge
gate: an unearned CHANGES REQUIRED wedges the pull request permanently on
nothing the author can act on, and an unearned APPROVE merges code nobody
read. A lock that is never reclaimed strands a repository until a human
notices.

The blocks are EXTRACTED from the runner rather than copied, so a restructure
fails loudly instead of leaving a test that passes against code nobody ships.
`curl` and `openclaw` are the fakes on PATH — no network, no model call.
"""

import json
import os
import shutil
import subprocess
import unittest

from harness import BUILDER, ShellTestCase

RUNNER = "reviewer-runner.sh"

API_START = "# -- the code host"
API_END = "# -- re-resolve the pull request"
VERDICT_START = "# -- deterministic verdict handling"
VERDICT_END = "# -- record what was reviewed"
LOCK_START = "# -- per-repo lock"
LOCK_END = "# -- shared concurrency slot"


class VerdictBlock(ShellTestCase):
    """What the wrapper posts — and, mostly, what it must not."""

    def setUp(self):
        super().setUp()
        self.api = self._block(API_START, API_END, "api.sh")
        self.verdict = self._block(VERDICT_START, VERDICT_END, "verdict.sh")
        self.fixtures = os.path.join(self.home, "fixtures")
        os.makedirs(self.fixtures, exist_ok=True)
        self.env["FAKE_FORGE_DIR"] = "$PWD/fixtures"
        self.env["FAKE_FORGE_LOG"] = "$PWD/forge.log"
        # No verdict comment on the change request yet — the ordinary state at
        # the moment a run finishes.
        self.fixture("comments_7", [])

    def _block(self, start, end, name):
        """extract_block always writes to the same path; keep both blocks."""
        src = self.extract_block(RUNNER, start, end)
        dst = os.path.join(self.home, name)
        shutil.move(src, dst)
        return name

    def fixture(self, slug, payload):
        with open(os.path.join(self.fixtures, slug), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(payload))

    def requests(self):
        path = os.path.join(self.home, "forge.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]

    def posts(self):
        """Everything the block SAID — a comment or a formal review.

        The rule this file defends is "an incomplete run posts NOTHING", and
        the verb names what posting means far more directly than the HTTP
        method ever did.
        """
        return [r for r in self.requests()
                if r.split()[0].split("_")[0] in ("comment",
                                                  "comment-on-change-request",
                                                  "submit-review")]

    def run_verdict(self, summary=None):
        if summary is not None:
            with open(os.path.join(self.home, "summary.md"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(summary)
        return self.sh(
            "set -u\n"
            "REPO=o/r\n"
            "PR_NUMBER=7\n"
            "GITHUB_TOKEN=token\n"
            "BOT_LOGIN=bot\n"
            'FORGE=(forge-cli --repo "$REPO")\n'
            'CR_NOUN="pull request"\n' 
            "HEAD_SHA=abc1234\n"
            "RUN_START_EPOCH=0\n"
            'SUMMARY_FILE="$PWD/summary.md"\n'
            'ATTEMPTS_FILE="$PWD/attempts"\n'
            "ATTEMPTS=0\n"
            'LOG_FILE="$PWD/run.log"\n'
            ": > \"$LOG_FILE\"\n"
            # Stand in for the bookkeeping that happens after the verdict —
            # not what this block decides.
            "record_reviewed() { echo RECORDED; }\n"
            "agent_run_hit_infra_error() { return 1; }\n"
            f'source "$PWD/{self.api}"\n'
            f'source "$PWD/{self.verdict}"\n'
            "echo REACHED_END\n")

    # -- the rule that must never be softened ---------------------------

    def test_an_incomplete_run_posts_NOTHING(self):
        # No verdict comment from the agent and no summary file: the review
        # did not happen. A guessed verdict here is worse than silence — it is
        # the solver's merge gate, and nothing the author does can clear one
        # that was never earned.
        rc, out, err = self.run_verdict()
        self.assertEqual(self.posts(), [], f"posted something: {self.posts()}")
        self.assertNotIn("REACHED_END", out)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("INCOMPLETE", out)

    def test_an_incomplete_run_leaves_the_sha_unrecorded(self):
        # Recording it would mean the planner never looks at this head again:
        # one provider outage, and the pull request is unreviewable forever.
        rc, out, _ = self.run_verdict()
        self.assertNotIn("RECORDED", out)
        with open(os.path.join(self.home, "attempts"), encoding="utf-8") as f:
            self.assertEqual(f.read().split(), ["abc1234", "1"])

    def test_an_incomplete_run_still_asked_whether_a_verdict_existed(self):
        # The silence must come from having looked, not from skipping the
        # lookup.
        self.run_verdict()
        self.assertTrue(
            any(r.startswith("comments") for r in self.requests()),
            self.requests())

    def test_a_provider_outage_is_named_as_such(self):
        # Same silence either way, but the log has to distinguish "the model
        # never answered" from "the agent reviewed and said nothing".
        rc, out, _ = self.sh(
            "set -u\nREPO=o/r\nPR_NUMBER=7\nGITHUB_TOKEN=t\nBOT_LOGIN=bot\n"
            "HEAD_SHA=abc1234\nRUN_START_EPOCH=0\n"
            'SUMMARY_FILE="$PWD/summary.md"\nATTEMPTS_FILE="$PWD/attempts"\n'
            'ATTEMPTS=0\nLOG_FILE="$PWD/run.log"\n: > "$LOG_FILE"\n'
            "record_reviewed() { echo RECORDED; }\n"
            "agent_run_hit_infra_error() { return 0; }\n"
            f'source "$PWD/{self.api}"\n'
            f'source "$PWD/{self.verdict}"\n')
        self.assertEqual(rc, 1)
        self.assertIn("provider error", out)
        self.assertEqual(self.posts(), [])

    # -- a completed run does post --------------------------------------

    def test_a_summary_with_findings_posts_the_comment_and_the_review(self):
        rc, out, err = self.run_verdict(
            "RESULT: changes required — 2 finding(s)\n\n1. a thing\n")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("REACHED_END", out)
        self.assertTrue(
            any(p.startswith("comment-on-change-request") for p in self.posts()),
            self.posts())
        self.assertTrue(any(p.startswith("submit-review") for p in self.posts()),
                        self.posts())
        self.assertIn("changes requested", out)

    def test_an_approved_summary_submits_an_APPROVE(self):
        rc, out, err = self.run_verdict("RESULT: approved\n\nall good\n")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("approved as a formal review", out)

    def test_a_refused_review_is_not_a_failed_run(self):
        # At least one host refuses a review from the change's own author,
        # and here that is always the case — the solver and the reviewer are
        # one account. The RESULT comment is the verdict; the formal review
        # is a nicety, and losing it must not fail the run or lose the
        # verdict that was already posted.
        self.env["FAKE_FORGE_FAIL"] = "submit-review"
        rc, out, err = self.run_verdict("RESULT: approved\n\nall good\n")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("not recorded", out)
        self.assertIn("REACHED_END", out)

    def test_a_comment_that_will_not_post_is_NOT_treated_as_a_verdict(self):
        # If the comment cannot be posted there is no machine-readable
        # verdict for the solver to read, so the run is incomplete and must
        # retry — not proceed to submit a review for a verdict nobody can see.
        # The write itself is refused, which is what "the verdict is not
        # visible" actually looks like: a read with no fixture would only
        # starve the lookup, not the posting.
        # The verdict goes on the CHANGE REQUEST, so that is the write to
        # refuse. `comment` is the issue verb and refusing it here would
        # prove nothing.
        self.env["FAKE_FORGE_FAIL"] = "comment-on-change-request"
        rc, out, err = self.run_verdict("RESULT: approved\n\nall good\n")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("INCOMPLETE", out)
        self.assertEqual(
            [p for p in self.posts() if p.startswith("submit-review")], [],
            "submitted a review for a verdict that was never posted")

    def test_the_agents_own_comment_is_taken_as_the_verdict(self):
        # The normal path: the agent posted it itself, so the wrapper posts
        # nothing and only submits the review event.
        self.fixture("comments_7", [{
            "author": {"username": "bot"},
            "created_at": "2099-01-01T00:00:00Z",
            "body": "🔎 REVIEW RESULT: APPROVED (sha abc1234)\n\n## Acceptance criteria\n1. ✅",
        }])
        rc, out, err = self.run_verdict()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([p for p in self.posts() if "comments" in p], [],
                         "posted a second verdict comment")
        self.assertIn("approved as a formal review", out)

    def test_a_verdict_for_another_sha_does_not_count(self):
        self.fixture("comments_7", [{
            "author": {"username": "bot"},
            "created_at": "2099-01-01T00:00:00Z",
            "body": "🔎 REVIEW RESULT: APPROVED (sha 9999999)",
        }])
        rc, out, _ = self.run_verdict()
        self.assertEqual(rc, 1)
        self.assertIn("INCOMPLETE", out)

    def test_a_verdict_from_an_earlier_run_does_not_count(self):
        # Matching any comment for the sha meant an earlier verdict was read
        # back as this run's result, so a head could never be re-reviewed.
        self.fixture("comments_7", [{
            "author": {"username": "bot"},
            "created_at": "1999-01-01T00:00:00Z",
            "body": "🔎 REVIEW RESULT: CHANGES REQUIRED (sha abc1234)",
        }])
        rc, out, _ = self.sh(
            "set -u\nREPO=o/r\nPR_NUMBER=7\nGITHUB_TOKEN=t\nBOT_LOGIN=bot\n"
            "HEAD_SHA=abc1234\n"
            f"RUN_START_EPOCH={2 ** 31}\n"
            'SUMMARY_FILE="$PWD/summary.md"\nATTEMPTS_FILE="$PWD/attempts"\n'
            'ATTEMPTS=0\nLOG_FILE="$PWD/run.log"\n: > "$LOG_FILE"\n'
            "record_reviewed() { echo RECORDED; }\n"
            "agent_run_hit_infra_error() { return 1; }\n"
            f'source "$PWD/{self.api}"\n'
            f'source "$PWD/{self.verdict}"\n')
        self.assertEqual(rc, 1)
        self.assertIn("INCOMPLETE", out)

    def test_somebody_elses_comment_is_not_the_bots_verdict(self):
        self.fixture("comments_7", [{
            "user": {"login": "a-human"},
            "created_at": "2099-01-01T00:00:00Z",
            "body": "🔎 REVIEW RESULT: APPROVED (sha abc1234)",
        }])
        rc, out, _ = self.run_verdict()
        self.assertEqual(rc, 1)
        self.assertIn("INCOMPLETE", out)


class LockReclaim(ShellTestCase):
    """One reviewer per repo — without deadlocking on a dead one's lock."""

    def setUp(self):
        super().setUp()
        src = self.extract_block(RUNNER, LOCK_START, LOCK_END)
        self.block = os.path.basename(src)
        self.lock = os.path.join(self.home, "lock")
        self.spare = []

    def tearDown(self):
        for p in self.spare:
            p.kill()
            p.wait()
        super().tearDown()

    def live_runner(self) -> int:
        """A live process whose /proc cmdline says it is a reviewer-runner."""
        p = subprocess.Popen(
            ["bash", "-c", "exec -a /usr/local/bin/reviewer-runner sleep 60"])
        self.spare.append(p)
        return p.pid

    def live_stranger(self) -> int:
        """A live process that is NOT a reviewer-runner — the PID-recycle case."""
        p = subprocess.Popen(["sleep", "60"])
        self.spare.append(p)
        return p.pid

    def hold(self, owner, age_seconds=0):
        os.makedirs(self.lock, exist_ok=True)
        with open(os.path.join(self.lock, "owner"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(f"{owner} 2026-08-17T09:00:00+00:00 pr=7\n")
        if age_seconds:
            stamp = f"-{age_seconds} seconds"
            subprocess.run(["touch", "-d", stamp, self.lock], check=True)

    def take(self, ttl=7200):
        rc, out, err = self.sh(
            "set -u\n"
            "REPO=o/r\nPR_NUMBER=7\n"
            'LOCK_DIR="$PWD/lock"\n'
            f"LOCK_TTL={ttl}\n"
            'LOG_FILE="$PWD/lock.log"\n'
            f'source "$PWD/{self.block}"\n'
            "echo TOOK_THE_LOCK\n")
        log = ""
        path = os.path.join(self.home, "lock.log")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                log = f.read()
        return rc, out, log

    def owner_pid(self):
        with open(os.path.join(self.lock, "owner"), encoding="utf-8") as f:
            return f.read().split()[0]

    def test_a_free_repo_is_locked(self):
        rc, out, log = self.take()
        self.assertEqual(rc, 0)
        self.assertIn("TOOK_THE_LOCK", out)
        self.assertTrue(os.path.isdir(self.lock))
        self.assertTrue(self.owner_pid().isdigit())

    def test_a_live_reviewer_keeps_its_lock(self):
        pid = self.live_runner()
        self.hold(pid)
        rc, out, log = self.take()
        self.assertEqual(rc, 0)
        self.assertNotIn("TOOK_THE_LOCK", out)
        self.assertIn("lock held", log)
        self.assertEqual(self.owner_pid(), str(pid))

    def test_a_dead_owner_is_reclaimed(self):
        # The deadlock this exists for: a runner SIGKILLed or lost to a pod
        # restart never runs its EXIT trap, and the planner keeps re-spawning
        # us. A plain mkdir-or-abort would strand the repo forever.
        self.hold(999999)
        rc, out, log = self.take()
        self.assertEqual(rc, 0)
        self.assertIn("TOOK_THE_LOCK", out)
        self.assertIn("stale reviewer lock", log)

    def test_a_live_stranger_does_not_hold_the_lock(self):
        # PIDs get recycled. A live PID that is not a reviewer-runner is not
        # an owner.
        self.hold(self.live_stranger())
        rc, out, log = self.take()
        self.assertIn("TOOK_THE_LOCK", out)
        self.assertIn("stale reviewer lock", log)

    def test_an_ownerless_lock_is_reclaimed(self):
        os.makedirs(self.lock, exist_ok=True)
        rc, out, log = self.take()
        self.assertIn("TOOK_THE_LOCK", out)

    def test_a_lock_past_the_TTL_is_reclaimed_even_with_a_live_owner(self):
        # A run that has held the lock for longer than a review can possibly
        # take is wedged, whatever its process table says.
        self.hold(self.live_runner(), age_seconds=9000)
        rc, out, log = self.take(ttl=7200)
        self.assertIn("TOOK_THE_LOCK", out)
        self.assertIn("stale reviewer lock", log)

    def test_a_young_lock_with_a_live_owner_survives_the_TTL_check(self):
        self.hold(self.live_runner(), age_seconds=60)
        rc, out, log = self.take(ttl=7200)
        self.assertNotIn("TOOK_THE_LOCK", out)
        self.assertIn("lock held", log)


if __name__ == "__main__":
    unittest.main()

"""A solver with nothing to do must give the slot back.

WHAT WAS FOUND RUNNING
Four fixer-runners alive in one pod, one working. Every global agent slot
taken, and every fresh spawn logging

    [slot] all 4 slots busy (held by: solver …#136 solver …#136 solver …#149 solver …#149)
    [slot] no agent slot free — yielding this tick (nothing recorded, next tick retries)
    [planning] recorded solver run: 0 model call(s)

Two independent defects produced that, and they compound.

1. THE IDLE POLL LOOP CAMPED.
   With no mention, no pull request and no closure, the loop slept five
   minutes and looked again — for as long as the run was allowed to live,
   which is solver.lifetime, six hours. All of it holding the repository's
   lock and one of the few global slots while doing nothing. The waiting
   bought responsiveness to an @-mention arriving mid-run; this bot is
   automatic and nobody converses with it on the change request, so it was
   latency nobody was waiting on, paid for in the scarcest resource there is.

2. THE LOCK TTL WAS SHORTER THAN THE RUN IT GUARDED.
   A flat hour against a six-hour lifetime, so a fixer past the hour had its
   lock declared stale and taken by a fresh spawn while it was still running:

       reclaiming stale lock for <repo> (age=3906s owner=1687); proceeding with #149

   Two runners on one issue, each holding a slot. The age test is there for a
   lock orphaned by SIGKILL or a pod restart, where no exit trap ran — but age
   cannot tell "orphaned" from "still working", so it has to sit beyond any
   legitimate run.

Nothing is lost by exiting: the cursor is preserved, and an answer posted
meanwhile is read by the next planner or solver run — the same path that
already handles every mention arriving between runs.
"""

import os
import re
import subprocess
import unittest

from harness import BUILDER

RUNNER = os.path.join(BUILDER, "fixer-runner.sh")


def src() -> str:
    with open(RUNNER, encoding="utf-8") as f:
        return f.read()


def poll_loop() -> str:
    """The loop body, so an assertion cannot pass on some other sleep."""
    s = src()
    start = s.index("# -- poll loop")
    return s[start:s.index("\ndone", start)]


def poll_loop_code() -> str:
    """The loop with comments stripped.

    The prose in and around this loop necessarily talks about the sleep that
    used to be here, so a search over the raw text would match its own
    explanation and never fail.
    """
    return "\n".join(l for l in poll_loop().splitlines()
                      if not l.lstrip().startswith("#"))


class AnIdleRunExits(unittest.TestCase):
    maxDiff = None

    def test_the_idle_branch_leaves_the_loop(self):
        block = poll_loop()
        m = re.search(r'if \[ -z "\$NEW_JSON" \].*?\n(.*?)\n  fi', block, re.S)
        self.assertIsNotNone(m, "the no-new-mentions branch is gone")
        self.assertIn("break", m.group(1),
                      "an idle solver must end the run, not loop again")
        self.assertNotIn("continue", m.group(1))

    def test_the_loop_no_longer_sleeps(self):
        # The whole defect in one line. A sleep here is a slot held for the
        # length of the sleep, repeated until the lifetime cap.
        self.assertNotIn("sleep", poll_loop_code())

    def test_the_dead_knob_is_gone(self):
        # It configured the sleep that no longer exists. Left behind it would
        # read as a working control that silently does nothing.
        self.assertNotIn("POLL_INTERVAL", src())

    def test_the_exit_says_the_slot_is_freed(self):
        # A run that ends is indistinguishable from one that crashed unless it
        # says so, and this one ends on purpose.
        self.assertRegex(poll_loop(), r"nothing to react to.*slot is free")

    def test_the_cursor_is_not_wiped_on_this_path(self):
        # Exiting is only free because the next run resumes where this one
        # stopped. WIPE_FULL_STATE is for a CLOSED issue and nothing else.
        block = poll_loop()
        idle = block[block.index("NOTHING TO REACT TO"):]
        self.assertNotIn("WIPE_FULL_STATE=1", idle)


class TheLockOutlivesTheRun(unittest.TestCase):
    maxDiff = None

    def ttl_expression(self) -> str:
        m = re.search(r'^LOCK_TTL="(.*)"$', src(), re.M)
        self.assertIsNotNone(m, "LOCK_TTL is gone")
        return m.group(1)

    def test_the_ttl_is_derived_from_the_lifetime(self):
        self.assertIn("MAX_LIFETIME_SECONDS", self.ttl_expression(),
                      "a hardcoded TTL drifts from the lifetime it guards")

    def test_the_lifetime_is_resolved_before_the_ttl_reads_it(self):
        # Ordering is the whole of it: read too early, the expansion is empty
        # and the TTL silently collapses to its fallback. The LAST assignment
        # is the one that matters — autoruntime rewrites the lifetime from the
        # story size, and a TTL derived before that would guard the wrong
        # number while still looking derived.
        s = src()
        ttl_at = s.index("\nLOCK_TTL=")
        assignments = [m.start() for m in
                       re.finditer(r"^\s*MAX_LIFETIME_SECONDS=", s, re.M)]
        self.assertTrue(assignments, "the lifetime is never assigned")
        self.assertLess(max(assignments), ttl_at,
                        "the lifetime is still being rewritten after the TTL "
                        "has been derived from it")

    def test_a_full_length_run_cannot_outlive_its_own_lock(self):
        # Evaluated, not read: the arithmetic is what protects a live runner
        # from having its lock taken.
        for lifetime in ("21600", "3600", "600"):
            with self.subTest(lifetime=lifetime):
                out = subprocess.run(
                    ["bash", "-c",
                     f'MAX_LIFETIME_SECONDS={lifetime}; '
                     f'LOCK_TTL="{self.ttl_expression()}"; echo "$LOCK_TTL"'],
                    capture_output=True, text=True, timeout=30)
                self.assertGreater(int(out.stdout.strip()), int(lifetime),
                                   "the lock expires while the run may still "
                                   "be going, so a second runner takes it")

    def test_liveness_is_still_the_real_signal(self):
        # The age test only exists for a lock nobody will ever release. A dead
        # owner must still be reclaimed at once, whatever the age says.
        s = src()
        block = s[s.index("LOCK_TTL="):s.index('echo "$BASHPID')]
        self.assertIn("kill -0", block,
                      "only age decides staleness, so a dead owner's lock "
                      "waits out the TTL before anyone may take it")
        self.assertIn("lock_age", block)


if __name__ == "__main__":
    unittest.main()

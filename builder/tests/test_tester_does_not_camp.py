"""A tester must end, and must not take its repository down with it.

TWO INCIDENTS, ONE RUN
Found alive on prod: `tester-runner group/team/app` at 13h54m and
`tester-runner group/team/web` at 11h35m, each with an orphaned
Chromium tree and a `node step-b2.mjs` stuck since minutes after its agent
had finished.

1. THE CAP ONLY COVERED THE MODEL.
   MAX_LIFETIME kills $AGENT_PID. `wait` then returns and the run walks on
   into drafts processing — screenshot uploads, issue filing, a headless
   browser — with no cap on any of it. The comment claimed it stopped the
   runner camping on the pod for an hour; it only ever stopped the model.
   So the backstop is armed for the WHOLE run, before the agent, and kills
   the process group rather than this shell alone: TERMing only the script
   leaves Chromium's tree parented to init, holding the pod's memory until
   the pod is replaced.

2. THE LOCK COULD NEVER BE RECOVERED.
   The tester took its per-repo lock with a plain mkdir-or-abort and no
   reclaim, while the fixer next door had TTL-and-liveness reclaim for
   exactly this reason. A tester killed without its EXIT trap firing — a
   SIGKILL, an evicted pod, or the new backstop above — left the directory
   behind, and nothing ever removed it. That repository is then never tested
   again, silently: every later tick logs "lock held" and moves on, which
   reads precisely like a tester that is busy working.

WHAT THESE TESTS ARE
Structural, asserted against the scripts' own source, in the manner of
test_scripts_from_configmap. The runners are 1,200-line shell programs whose
real execution needs a cluster, a model and a browser; what can be checked
cheaply and repeatedly is that the guards are present, that they are armed
in an order that actually covers the gap, and that the two subsystems have
not drifted apart again.
"""

from __future__ import annotations

import os
import unittest

from harness import BUILDER

FIXER = os.path.join(BUILDER, "fixer-runner.sh")
TESTER = os.path.join(BUILDER, "tester-runner.sh")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TheWholeRunIsCapped(unittest.TestCase):
    def setUp(self):
        self.src = read(TESTER)

    def test_there_is_a_whole_run_backstop(self):
        self.assertIn("MAX_RUN_SECONDS", self.src)

    def test_the_backstop_is_armed_before_the_agent_is_waited_on(self):
        # THE bug: a cap that starts after the model turn cannot cover the
        # phase that actually hung. Order is the whole point of the fix.
        armed = self.src.index("MAX_RUN_SECONDS")
        waited = self.src.index('wait "$AGENT_PID"')
        self.assertLess(
            armed, waited,
            "the whole-run cap is armed after the agent, so it cannot cover "
            "drafts processing - which is where prod actually wedged")

    def test_it_kills_the_process_group_not_just_this_shell(self):
        # Chromium's renderers outlive a TERM sent only to the script.
        self.assertIn('kill -KILL -"$RUN_PGID"', self.src)

    def test_the_whole_run_gets_at_least_as_long_as_the_model(self):
        # A total cap SHORTER than the agent's own would kill healthy runs
        # mid-turn, which trades a silent hang for silent data loss.
        self.assertIn("MAX_LIFETIME_SECONDS:-3600} * 2", self.src)

    def test_the_trap_still_releases_everything(self):
        # The backstop TERMs the main shell and waits before the KILL, so the
        # EXIT trap has to be the thing that drops the lock and the slot.
        self.assertIn("RUN_WATCHER_PID", self.src)
        cleanup = self.src[self.src.index("cleanup() {"):]
        cleanup = cleanup[:cleanup.index("trap cleanup EXIT")]
        self.assertIn('rm -rf "$LOCK_DIR"', cleanup)
        self.assertIn("release_agent_slot", cleanup)


class TheTesterLockCanBeReclaimed(unittest.TestCase):
    def setUp(self):
        self.src = read(TESTER)

    def test_a_stale_lock_is_reclaimed(self):
        self.assertIn("reclaiming stale tester lock", self.src)

    def test_reclaim_tests_both_age_and_liveness(self):
        # Age alone strands a repo for the whole TTL after a crash; liveness
        # alone strands it forever if the PID is recycled. Both, as the fixer
        # already does.
        self.assertIn("$lock_age", self.src)
        self.assertIn('kill -0 "$lock_pid"', self.src)

    def test_the_owner_file_records_the_pid_first(self):
        # The reclaim reads field 1 of the owner file with awk; a format
        # change here silently disables the liveness half.
        self.assertIn('echo "$BASHPID $(date -Iseconds)" > "$LOCK_DIR/owner"',
                      self.src)


class TheTwoSubsystemsHaveNotDrifted(unittest.TestCase):
    """The tester lacked this guard for as long as the fixer had it. Asserting
    on both is what stops the next one being fixed alone."""

    def test_both_runners_reclaim_a_stale_lock(self):
        for path in (FIXER, TESTER):
            src = read(path)
            with self.subTest(runner=os.path.basename(path)):
                self.assertIn("kill -0 \"$lock_pid\"", src)
                self.assertIn("LOCK_TTL", src)

    def test_both_runners_write_an_owner_file(self):
        for path in (FIXER, TESTER):
            with self.subTest(runner=os.path.basename(path)):
                self.assertIn('"$LOCK_DIR/owner"', read(path))


if __name__ == "__main__":
    unittest.main()

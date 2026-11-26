"""estimate-batch: a repo's unsized backlog, sized one after another.

The planner hands every unsized issue of a repo to ONE batch (heartbeat-issue
-tick, `toEstimate`) and holds that repo's fresh work until the batch is done.
Two properties carry that design, and both fail silently when broken:

  * sequential, and all of them — a batch that stopped at the first failing
    issue would leave the rest unsized, and fresh work waiting on them forever;
  * one batch per repo — the lock the planner reads. Without it, a slow batch
    is doubled by the next tick and every issue is sized twice.

Run against a real bash with a fake estimate-runner on PATH that records which
issue it was asked to size, in what order.
"""

import os
import unittest

from harness import BUILDER, ShellTestCase, bash_path, load_script

BATCH = os.path.join(BUILDER, "estimate-batch.sh")
REPO = "acme/team/web-test"
LOCK = ".openclaw/.estimate-locks/acme__team__web-test"


class EstimateBatch(ShellTestCase):

    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.home, "sized.txt")
        runner = os.path.join(self.bin, "estimate-runner")
        with open(runner, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/bash\n"
                    'echo "$1#$2" >> "$HOME/sized.txt"\n'
                    # issue 13 is the one that fails
                    '[ "$2" = 13 ] && exit 1\n'
                    "exit 0\n")
        os.chmod(runner, 0o755)

    def batch(self, *issues):
        args = " ".join([REPO, *map(str, issues)])
        return self.sh(f"bash {bash_path(BATCH)} {args}")

    def sized(self):
        if not os.path.exists(self.record):
            return []
        with open(self.record, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]

    def test_every_issue_is_sized_in_the_order_given(self):
        rc, _, _ = self.batch(3, 1, 2)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sized(), [f"{REPO}#3", f"{REPO}#1", f"{REPO}#2"])

    def test_a_failing_estimate_does_not_stop_the_rest(self):
        self.batch(1, 13, 2)
        self.assertEqual(self.sized(), [f"{REPO}#1", f"{REPO}#13", f"{REPO}#2"])

    def test_the_lock_is_released_when_the_batch_ends(self):
        self.batch(1)
        self.assertFalse(os.path.exists(os.path.join(self.home, LOCK)))

    def test_a_live_batch_is_not_doubled(self):
        lock = os.path.join(self.home, LOCK)
        os.makedirs(lock)
        # A process that is certainly alive inside the bash this test runs.
        rc, out, _ = self.sh(
            f"sleep 30 & pid=$!; echo \"$pid estimate-batch\" > {bash_path(lock)}/owner; "
            f"bash {bash_path(BATCH)} {REPO} 1; kill $pid")
        self.assertEqual(rc, 0)
        self.assertEqual(self.sized(), [])

    def test_a_stale_lock_is_taken_over(self):
        # Owner gone: the pod restarted mid-batch. That is normal, and must
        # not wedge sizing for the repo until somebody deletes a directory.
        lock = os.path.join(self.home, LOCK)
        os.makedirs(lock)
        with open(os.path.join(lock, "owner"), "w") as f:
            f.write("999999 estimate-batch\n")
        self.batch(1)
        self.assertEqual(self.sized(), [f"{REPO}#1"])

    def test_no_issues_is_a_usage_error(self):
        rc, _, _ = self.sh(f"bash {bash_path(BATCH)} {REPO}")
        self.assertNotEqual(rc, 0)


class LockNamesOfNestedProjects(ShellTestCase):
    """The planner reads lock dir names back into repo full names.

    Every `/` is written as `__`, so a GitLab subgroup path acme/team/web is the
    dir acme__team__web. Turning only the FIRST `__` back into `/` produced
    acme/team__web, which matches no repo — the planner never saw a held lock on
    a nested project. Run against a real bash and a real lock tree.
    """

    def held(self, root_name, via=None):
        tick = load_script("heartbeat-issue-tick.py")

        def run(namespace, pod, *cmd, timeout=15):
            # Through the harness, which exports HOME and PATH inside bash —
            # an inherited Windows HOME is not what bash sees.
            return self.sh(cmd[-1])

        saved = tick.kubectl_exec_capture
        tick.kubectl_exec_capture = run
        try:
            if via:
                return getattr(tick, via)("ns", "pod")
            return tick._list_held_locks("ns", "pod", root_name)
        finally:
            tick.kubectl_exec_capture = saved

    def test_a_subgroup_path_is_read_back_whole(self):
        for root in (".fixer-locks", ".estimate-locks"):
            with self.subTest(root):
                os.makedirs(os.path.join(self.home, ".openclaw", root,
                                         "acme__team__claw-code-web-test"))
                os.makedirs(os.path.join(self.home, ".openclaw", root,
                                         "owner__name"))
                self.assertEqual(
                    self.held(root),
                    {"acme/team/claw-code-web-test", "owner/name"})


    def test_the_planner_reads_estimate_batches_from_their_own_dir(self):
        # list_estimating_repos is what holds a repo's fresh work while its
        # batch runs; a fixer lock must not be mistaken for a batch.
        os.makedirs(os.path.join(self.home, ".openclaw", ".estimate-locks",
                                 "acme__team__web"))
        os.makedirs(os.path.join(self.home, ".openclaw", ".fixer-locks",
                                 "o__fixing"))
        self.assertEqual(self.held(None, via="list_estimating_repos"),
                         {"acme/team/web"})
        self.assertEqual(self.held(None, via="list_locked_repos"),
                         {"o/fixing"})


if __name__ == "__main__":
    unittest.main()

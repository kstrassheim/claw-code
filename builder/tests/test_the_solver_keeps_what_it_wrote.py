"""Work that only exists locally is still work.

WHAT WAS HAPPENING
With no pull request open, every run began:

    git reset --hard origin/<default>
    git clean -fdx
    git branch -D <branch>

unconditionally. So a run that committed but died before pushing — the
lifetime cap, a provider 403, a pod restart — had its commits deleted by the
NEXT run, which then started the same issue from nothing.

Found in a live checkout: `Fix /ws/worldline-status role check (issue #124)`,
a real fix touching an API and its tests, dangling and unreachable, while
#124 was still open and labelled in-progress. Issue #141 had four separate
attempts discarded the same way. Each cost a whole agent run and left no
trace on the issue, so from the outside the bot simply never converged.

Nothing was pushed and nothing was reported. The work was made and then
quietly thrown away by the next tick.

TWO CHANGES, AND THE SECOND IS WHY THE FIRST IS ENOUGH
1. A branch that is ahead of the default branch is RESUMED, not deleted. The
   working tree is left alone, so uncommitted edits survive too.
2. The exit trap commits whatever is still uncommitted on our branch, so the
   thing the resume finds is a commit rather than a dirty tree that some
   later `clean` could still take.

The reset path is kept for the case it was written for: no branch, or a
branch with nothing on it. There is nothing to lose there, and a poisoned
tree still gets a clean start.
"""

import os
import re
import subprocess
import tempfile
import shutil
import unittest

from harness import BUILDER, TMP_ROOT

RUNNER = os.path.join(BUILDER, "fixer-runner.sh")


def src() -> str:
    with open(RUNNER, encoding="utf-8") as f:
        return f.read()


def checkout_block() -> str:
    s = src()
    start = s.index('if [ -n "$EXISTING_PR_BRANCH" ]')
    return s[start:s.index("\n# -- gather issue context", start)]


def code_only(block: str) -> str:
    """The block with comments stripped.

    The prose here necessarily names the very commands it explains — the
    comment says the old path ran `reset --hard`, `clean -fdx`, `branch -D` —
    so an assertion over the raw text matches its own explanation and can
    never fail.
    """
    return "\n".join(l for l in block.splitlines()
                      if not l.lstrip().startswith("#"))


def func(name: str) -> str:
    s = src()
    start = s.index(f"{name}() {{")
    return s[start:s.index("\n}", start) + 2]


class ABranchWithWorkIsResumed(unittest.TestCase):
    maxDiff = None

    def test_the_destructive_reset_is_no_longer_unconditional(self):
        block = checkout_block()
        # The reset must sit behind a branch that has already established
        # there is nothing to lose.
        block = code_only(block)
        reset_at = block.index("git reset --hard")
        guard = block[:reset_at]
        self.assertIn("rev-list --count", guard,
                      "nothing counts the commits before deleting the branch")

    def test_a_branch_ahead_of_the_default_is_checked_out_not_deleted(self):
        block = checkout_block()
        # Stop at the next branch of the chain, not merely at `else`: the
        # remote-resume arm below legitimately deletes a stale local ref
        # before recreating it from origin, and running past it would read
        # that as this arm deleting the work it just resumed.
        m = re.search(r'rev-list --count "origin/\$DEFAULT_BRANCH\.\.\$BRANCH".*?\n(.*?)\n(?:elif|else)\b',
                      block, re.S)
        self.assertIsNotNone(m, "the resume branch is gone")
        resumed = code_only(m.group(1))
        self.assertIn('git checkout --quiet "$BRANCH"', resumed)
        self.assertNotIn("branch -D", resumed)
        self.assertNotIn("reset --hard", resumed)
        self.assertNotIn("clean -fdx", resumed,
                         "cleaning the tree on resume throws away the "
                         "uncommitted half of the work")

    def test_the_fresh_path_still_exists_for_an_empty_branch(self):
        # The reset is not the bug; doing it unconditionally was. A branch
        # with nothing on it, or a poisoned tree, still gets a clean start.
        block = checkout_block()
        tail = code_only(block[block.rindex("else"):])
        for expected in ("reset --hard", "clean -fdx", "branch -D"):
            self.assertIn(expected, tail)


class TheExitTrapSavesUnfinishedWork(unittest.TestCase):
    maxDiff = None

    def test_the_trap_calls_the_autosave(self):
        body = func("on_exit")
        self.assertIn("autosave_wip", body)

    def test_it_saves_before_anything_else_in_the_trap(self):
        # A later step in the trap must not become the reason work is lost.
        body = func("on_exit")
        self.assertLess(body.index("autosave_wip"), body.index("record_delivery"))

    def test_a_closed_issue_is_not_autosaved(self):
        # WIPE_FULL_STATE means the issue is finished; committing to a branch
        # nobody will read is noise on the volume.
        self.assertIn('[ "$WIPE_FULL_STATE" = "1" ] || autosave_wip', func("on_exit"))

    def test_it_carries_its_own_identity(self):
        # The pod has no global git identity — the agent sets one per
        # repository, and on a fresh clone that has not happened yet, which is
        # exactly when a run is most likely to die early.
        body = func("autosave_wip")
        self.assertIn("user.name=", body)
        self.assertIn("user.email=", body)

    def test_it_says_it_is_a_stopgap(self):
        # The message is read by whoever picks the branch up. It must not look
        # like the agent's own considered commit.
        self.assertIn("WIP (autosaved)", func("autosave_wip"))


class TheWorkOutlivesThisPod(unittest.TestCase):
    """A commit only this pod can see is one clean, or one replaced pod, from
    gone — and the checkout is SHARED: the next issue in the same repository
    works the same tree on its own branch."""

    def test_the_autosave_pushes(self):
        self.assertIn('push --quiet origin "HEAD:$BRANCH"', func("autosave_wip"))

    def test_the_push_is_never_forced(self):
        # If the remote moved, those commits belong to someone else — a person
        # pushing, or a pull request open on this branch. Ours stay local and
        # the resume still finds them.
        body = func("autosave_wip")
        self.assertNotIn("--force", body)
        self.assertNotIn("+HEAD", body)

    def test_a_failed_push_does_not_fail_the_run(self):
        # This is an exit trap. Nothing in it may become the reason a run
        # reports failure.
        body = func("autosave_wip")
        push = body[body.index("push --quiet"):]
        self.assertIn("kept locally", push)

    def test_a_branch_only_on_the_remote_is_resumed(self):
        block = code_only(checkout_block())
        self.assertIn('git ls-remote --heads origin "$BRANCH"', block)
        m = re.search(r'ls-remote --heads origin "\$BRANCH".*?\n(.*?)\nelse',
                      block, re.S)
        self.assertIsNotNone(m, "the remote-resume branch is gone")
        self.assertIn('-b "$BRANCH" "origin/$BRANCH"', m.group(1))

    def test_a_resumed_branch_rescues_what_the_last_run_left_loose(self):
        # A run killed outright never reaches its exit trap, so its edits are
        # still sitting uncommitted when the next run picks the branch up.
        # Committing and pushing them first means this run continues from
        # something safe on the remote — same branch, same issue.
        block = code_only(checkout_block())
        m = re.search(r'rev-list --count "origin/\$DEFAULT_BRANCH\.\.\$BRANCH".*?\n(.*?)\n(?:elif|else)\b',
                      block, re.S)
        self.assertIn("autosave_wip", m.group(1))

    def test_the_remote_resume_is_tried_before_starting_over(self):
        block = code_only(checkout_block())
        self.assertLess(block.index("ls-remote --heads origin"),
                        block.index("git reset --hard"))


class TheAutosaveBehavesInARealRepository(unittest.TestCase):
    """Run the extracted function against real git, not a reading of it."""

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="autosave-", dir=TMP_ROOT)
        self.repo = os.path.join(self.dir, "repo")
        self._git("init", "-q", self.repo, cwd=self.dir)
        self._git("config", "user.email", "t@t"), self._git("config", "user.name", "t")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("one\n")
        self._git("add", "-A"); self._git("commit", "-qm", "base")
        self._git("checkout", "-qb", "issue-1-fix")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _git(self, *args, cwd=None):
        return subprocess.run(("git",) + args, cwd=cwd or self.repo,
                              capture_output=True, text=True, timeout=60)

    def run_autosave(self, branch="issue-1-fix"):
        script = "\n".join([
            "set -u",
            f'PROJECT_DIR="{self.repo}"',
            f'BRANCH="{branch}"',
            'ISSUE_NUM=1',
            'BOT_LOGIN=testbot',
            func("autosave_wip"),
            "autosave_wip",
        ])
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=60)

    def head_subject(self):
        return self._git("log", "-1", "--format=%s").stdout.strip()

    def test_a_dirty_tree_becomes_a_commit(self):
        with open(os.path.join(self.repo, "b.txt"), "w") as f:
            f.write("new work\n")
        out = self.run_autosave()
        self.assertIn("autosave", out.stdout, out.stdout + out.stderr)
        self.assertIn("WIP (autosaved)", self.head_subject())
        self.assertEqual(self._git("status", "--porcelain").stdout.strip(), "")

    def test_untracked_files_are_included(self):
        # `clean -fdx` was what erased these, so saving only tracked changes
        # would leave the actual loss untouched.
        os.makedirs(os.path.join(self.repo, "sub"))
        with open(os.path.join(self.repo, "sub", "new.py"), "w") as f:
            f.write("print('hi')\n")
        self.run_autosave()
        files = self._git("show", "--name-only", "--format=", "HEAD").stdout
        self.assertIn("sub/new.py", files)

    def test_a_clean_tree_is_left_alone(self):
        before = self._git("rev-parse", "HEAD").stdout.strip()
        self.run_autosave()
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), before,
                         "a completed run must reach this as a no-op")

    def test_it_refuses_when_head_is_some_other_branch(self):
        # Never commit onto a branch this run does not own.
        self._git("checkout", "-q", "master") if self._git(
            "rev-parse", "--verify", "-q", "master").returncode == 0 else \
            self._git("checkout", "-q", "main")
        with open(os.path.join(self.repo, "c.txt"), "w") as f:
            f.write("stray\n")
        self.run_autosave(branch="issue-1-fix")
        self.assertNotIn("WIP (autosaved)", self.head_subject())
        self.assertNotEqual(self._git("status", "--porcelain").stdout.strip(), "",
                            "the stray change should still be sitting there")


if __name__ == "__main__":
    unittest.main()

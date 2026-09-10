"""A run takes its dev servers with it.

WHAT WAS HAPPENING
The agent starts servers to test against. On a frontend repository that is
`npm run test:frontend` -> cross-env -> vite, holding a port. Those are the
AGENT's children, not the runner's, and `on_exit` ended everything EXCEPT
them: autosave, record_delivery, planning-record, the slot, AGENT_TURN_OUT,
LOCK_DIR — and nothing for the process the agent forked.

So they reparented to PID 1 and stayed until the pod was replaced. Found in
prod on 2026-09-09 after one day of runs on one repository: five trees, twenty
processes, ~1.7GB resident, every root orphaned, every one still holding port
5175.

WHY THE PORT IS THE EXPENSIVE PART, NOT THE MEMORY
They all want 5175. The first survivor keeps it and a later run cannot bind,
so the agent tests against a server still serving a PREVIOUS attempt's code
and is told — convincingly, with passing output — that its own edits changed
nothing. That is a wrong answer rather than an error, and the run that
believes it burns its whole budget learning nothing.

THE TWO THINGS THIS PINS
1. The agent runs in its OWN process group, and the group is killed on the way
   out of every turn and again in the exit trap.
2. The kill can never land on the runner's own group. If setsid is missing the
   agent shares this script's group, and signalling it would kill the runner
   mid-trap — losing the autosave, the delivery record and the slot. A leaked
   server is much the cheaper failure, so that case must leak rather than
   fire.

The signal spelling is pinned too, and it is not cosmetic: `kill -TERM --
"-$pgid"` is what one reaches for and dash rejects it (`kill: Illegal number:
-`), so the signal is never sent and the leak returns, silently.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from harness import BUILDER, TMP_ROOT

RUNNER = os.path.join(BUILDER, "fixer-runner.sh")


def src() -> str:
    with open(RUNNER, encoding="utf-8") as f:
        return f.read()


def kill_fn() -> str:
    s = src()
    start = s.index("kill_agent_group() {")
    return s[start:s.index("\n}", start)]


def turn_fn() -> str:
    s = src()
    start = s.index("run_agent_turn() {")
    return s[start:s.index("\n}", start)]


def code_only(block: str) -> str:
    """The block with comment lines stripped, so prose cannot satisfy a test."""
    return "\n".join(l for l in block.splitlines()
                     if not l.lstrip().startswith("#"))


class TheAgentRunsInItsOwnGroup(unittest.TestCase):
    def test_the_turn_is_wrapped_in_setsid(self):
        code = code_only(turn_fn())
        self.assertIn("$SETSID_BIN", code)
        self.assertIn("openclaw agent --local", code)

    def test_the_group_id_is_published_before_the_agent_execs(self):
        # A process in the middle of a pipeline has no $! to ask for, so the
        # wrapper writes its own pid — which exec preserves — to a file.
        code = code_only(turn_fn())
        self.assertIn("echo $$ >", code)
        self.assertIn('exec "$@"', code)
        self.assertIn("AGENT_PGID_FILE", code)

    def test_a_missing_setsid_still_runs_the_agent(self):
        # Degrading to the old behaviour is correct; refusing to run is not.
        code = code_only(turn_fn())
        self.assertIn('if [ -n "$SETSID_BIN" ]; then', code)
        self.assertEqual(code.count("openclaw agent --local"), 2,
                         "the fallback branch must still invoke the agent")

    def test_the_exit_code_is_still_the_agents(self):
        # PIPESTATUS[0], not tee's status, in BOTH branches — the poll loop
        # reads this to tell a fatal failure from a transient one.
        code = code_only(turn_fn())
        self.assertEqual(code.count("rc=${PIPESTATUS[0]}"), 2)

    def test_setsid_wait_is_probed_rather_than_assumed(self):
        # Bare setsid forks when its caller is a group leader and then returns
        # 0 immediately, losing the agent's status. --wait removes the "when".
        # A setsid too old for the flag must degrade, not fail every turn.
        s = src()
        self.assertIn('"$SETSID_BIN" --wait true', s)
        self.assertIn('SETSID_WAIT="--wait"', s)
        self.assertIn('"$SETSID_BIN" $SETSID_WAIT', s)


class TheGroupIsKilled(unittest.TestCase):
    def test_every_turn_cleans_up_after_itself(self):
        # Not only the last turn: a server left by turn 2 holds the port for
        # the rest of the run.
        self.assertIn("kill_agent_group", code_only(turn_fn()))

    def test_the_exit_trap_also_kills_it(self):
        s = src()
        trap = s[s.index("on_exit() {"):s.index("\ntrap on_exit EXIT")]
        self.assertIn("kill_agent_group", code_only(trap))

    def test_the_autosave_still_comes_first(self):
        # The group holds servers, not work — but the ordering rule in this
        # trap is that nothing runs before the thing that saves the work.
        trap = code_only(src()[src().index("on_exit() {"):
                               src().index("\ntrap on_exit EXIT")])
        self.assertLess(trap.index("autosave_wip"),
                        trap.index("kill_agent_group"))

    def test_the_signal_is_spelled_the_way_dash_accepts(self):
        # `kill -TERM -- "-$pgid"` raises `Illegal number: -` in dash, so the
        # signal is never sent and nothing says so. Verified in the runtime
        # image against both shells.
        code = code_only(kill_fn())
        self.assertNotIn('kill -TERM -- ', code)
        self.assertNotIn('kill -0 -- ', code)
        self.assertNotIn("kill -s ", code)
        self.assertIn('kill -TERM "-$pgid"', code)
        self.assertIn('kill -0 "-$pgid"', code)

    def test_it_insists_when_asked_nicely_does_not_work(self):
        code = code_only(kill_fn())
        self.assertIn('kill -KILL "-$pgid"', code)


class ItNeverKillsTheRunner(unittest.TestCase):
    def test_the_runners_own_group_is_refused(self):
        code = code_only(kill_fn())
        self.assertIn('[ "$pgid" = "$own" ]', code)
        self.assertIn('[ "$pgid" = "$$" ]', code)

    def test_the_own_group_is_read_without_needing_procps(self):
        # `ps` is not installed in every image this can run in — the CI image
        # has no procps, which is how the first version of this file failed.
        # Reading it only from `ps` would leave the guard silently empty
        # exactly where it is the last thing standing between the trap and
        # killing its own runner.
        code = code_only(kill_fn())
        self.assertIn("/proc/self/stat", code)
        self.assertLess(code.index("/proc/self/stat"), code.index("ps -o pgid="),
                        "/proc must be tried before ps, not after")
        self.assertNotIn("cut -d' ' -f5", code,
                         "comm can contain spaces and parens; field 5 is not safe")

    def test_a_non_numeric_or_absent_group_is_refused(self):
        code = code_only(kill_fn())
        self.assertIn('case "$pgid" in ""|*[!0-9]*) return 0 ;; esac', code)
        self.assertIn('[ "$pgid" -gt 1 ]', code)


@unittest.skipUnless(shutil.which("setsid") and shutil.which("bash")
                     and os.path.isdir("/proc"),
                     "needs setsid, bash and /proc (absent on the Windows host)")
class TheMechanismActuallyWorks(unittest.TestCase):
    """Run the real thing: leak two children, then reap them by group.

    Structure tests above prove the code says the right words. This proves the
    words do the job — which is the half that was wrong the first time, when
    the `--` spelling parsed fine to a reader and sent no signal at all.

    NOTHING OUTSIDE THE SHELL IS USED TO OBSERVE IT. The first version of this
    counted group members with `ps`, which is not installed in the CI image —
    procps is not a base package — so it reported "before=0", failed, and
    would have been "fixed" by adding ps to the skip guard. That would have
    left the only test that exercises the mechanism skipped in the one place
    it matters. `/proc/<pid>` and `kill -0` are builtins-and-kernel, present
    wherever this code can run at all.
    """

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="pgroup-", dir=TMP_ROOT)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _run(self, script: str) -> str:
        p = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=120)
        return p.stdout + p.stderr

    def test_children_the_agent_left_are_reaped(self):
        pf = os.path.join(self.dir, "pgid")
        cf = os.path.join(self.dir, "kids")
        out = self._run(f"""
            set -uo pipefail
            alive() {{ n=0; for p in $(cat {cf!r}); do [ -d "/proc/$p" ] && n=$((n+1)); done; echo "$n"; }}
            setsid --wait sh -c 'echo $$ >"$1"; shift; exec "$@"' sh {pf!r} \\
              sh -c 'sleep 60 >/dev/null 2>&1 & echo $! >>{cf!r}
                     sleep 60 >/dev/null 2>&1 & echo $! >>{cf!r}
                     echo agent-ran' \\
              2>&1 | cat >/dev/null
            pgid=$(tr -d ' \\n' < {pf!r})
            echo "before=$(alive)"
            kill -0 "-$pgid" 2>/dev/null && echo "group-signalable"
            kill -TERM "-$pgid" 2>/dev/null && echo sent
            sleep 1
            echo "after=$(alive)"
            kill -0 "-$pgid" 2>/dev/null || echo "group-gone"
        """)
        # The leak, reproduced: the agent is gone, its two children are not.
        self.assertIn("before=2", out, out)
        self.assertIn("group-signalable", out, out)
        self.assertIn("sent", out, out)
        # ...and reaped by one signal to the group.
        self.assertIn("after=0", out, out)
        self.assertIn("group-gone", out, out)

    def test_the_dash_hostile_spelling_would_have_sent_nothing(self):
        # The regression this file exists for. If `kill -- -PGID` ever looks
        # like a tidier way to write it, this says what it costs.
        out = self._run(
            "sh -c 'kill -0 -- -1 2>&1' || true")
        self.assertRegex(out.lower(), r"illegal number|invalid|usage",
                         "dash is expected to reject the `--` form")

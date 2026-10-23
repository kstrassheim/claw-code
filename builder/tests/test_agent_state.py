"""Every agent turn on a state directory of its own (agent-state.sh).

openclaw 2026.10 refuses `agent --local` while a Gateway runs on the same state
directory, and the Gateway always runs in this pod on ~/.openclaw. After the
v2026.10.4 upgrade every estimate, solver, reviewer and tester turn died on
that refusal before the model was called — with a healthy pod and green
CronJobs, so nothing said so except an estimate log reading "produced no
POINTS line" every five minutes.

What has to hold:
  - a turn's state directory is NOT ~/.openclaw, and its config and workspace
    ARE the Gateway's (both default to paths under the state directory);
  - turns of one session share a directory, so the solver's later turns
    continue the first;
  - the prefix does not change the PID the runners signal and wait on;
  - every runner's `openclaw agent --local` actually carries the prefix.
"""

import os
import re
import unittest

from harness import BUILDER, ShellTestCase

LIB = ". $PWD/bin/agent-state.sh"
SHOW = ('"${AGENT_STATE_ENV[@]}" sh -c '
        "'echo \"$OPENCLAW_STATE_DIR|$OPENCLAW_CONFIG_PATH|$OPENCLAW_WORKSPACE_DIR\"'")


class TheTurnsState(ShellTestCase):
    def setUp(self):
        super().setUp()
        self.env["AGENT_STATE_RUNS_DIR"] = "$PWD/runs"

    def _show(self, session: str) -> list[str]:
        rc, out, err = self.sh(f'{LIB}\nagent_state_env "{session}"\n{SHOW}')
        self.assertEqual(rc, 0, err)
        return out.strip().split("|")

    def test_state_is_its_own_and_config_and_workspace_are_the_gateways(self):
        state, config, workspace = self._show("issue-x-1-100")
        self.assertEqual(state, os.path.join(self.home, "runs", "issue-x-1-100"))
        self.assertTrue(os.path.isdir(state))
        self.assertEqual(config, os.path.join(self.home, ".openclaw", "openclaw.json"))
        self.assertEqual(workspace, os.path.join(self.home, ".openclaw", "workspace"))

    def test_one_session_one_directory(self):
        a1 = self._show("issue-x-1-100")[0]
        a2 = self._show("issue-x-1-100")[0]
        b = self._show("review-x-2-200")[0]
        self.assertEqual(a1, a2, "a later turn must find the first turn's session")
        self.assertNotEqual(a1, b)

    def test_a_session_id_cannot_leave_the_runs_directory(self):
        state = self._show("../../etc x/y")[0]
        runs = os.path.join(self.home, "runs")
        self.assertEqual(os.path.dirname(state), runs)
        self.assertNotIn("/", os.path.basename(state))

    def test_the_prefix_keeps_the_pid(self):
        # The runners signal $! and the group setsid makes; `env` must exec in
        # place rather than leave itself in between.
        rc, out, err = self.sh(
            f'{LIB}\nagent_state_env s1\n'
            '"${AGENT_STATE_ENV[@]}" sh -c \'echo $$ > child\' &\n'
            'echo $! > parent; wait\n'
            'echo "$(cat parent) $(cat child)"')
        self.assertEqual(rc, 0, err)
        parent, child = out.split()
        self.assertEqual(parent, child)

    def test_idle_sessions_are_swept_and_the_current_one_is_not(self):
        rc, _, err = self.sh(
            "mkdir -p runs/old runs/current\n"
            "touch -t 202001010000 runs/old runs/current\n"
            f"{LIB}\nagent_state_env current", AGENT_STATE_MAX_IDLE_MIN=60)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.home, "runs", "old")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "runs", "current")),
                        "a turn must refresh its own session before sweeping")

    def test_without_the_library_the_call_still_runs(self):
        # The runners source it optionally; an image without it must still
        # run the turn (and be refused) rather than die on an unset array.
        rc, out, err = self.sh(
            "set -u\nAGENT_STATE_ENV=()\n"
            '${AGENT_STATE_ENV[@]+"${AGENT_STATE_ENV[@]}"} echo ran')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "ran")


class EveryRunnerUsesIt(unittest.TestCase):
    RUNNERS = ("fixer-runner.sh", "reviewer-runner.sh",
               "tester-runner.sh", "estimate-runner.sh")

    def test_every_local_turn_carries_the_prefix(self):
        for name in self.RUNNERS:
            with open(os.path.join(BUILDER, name), encoding="utf-8") as f:
                lines = f.read().splitlines()
            calls = [i for i, l in enumerate(lines)
                     if re.search(r"\bopenclaw agent --local\b", l)
                     and not l.lstrip().startswith("#")]
            with self.subTest(runner=name):
                self.assertTrue(calls, f"{name} has no `openclaw agent --local`")
                for i in calls:
                    # The prefix sits on the command's first line, which is
                    # this one or the continued line above it.
                    head = lines[i]
                    if i and lines[i - 1].rstrip().endswith("\\"):
                        head = lines[i - 1] + head
                    self.assertIn("AGENT_STATE_ENV", head,
                                  f"{name}:{i + 1} runs --local on the Gateway's state")

    def test_every_runner_prepares_the_state_before_its_turn(self):
        for name in self.RUNNERS:
            with open(os.path.join(BUILDER, name), encoding="utf-8") as f:
                text = f.read()
            with self.subTest(runner=name):
                self.assertRegex(text, r'agent_state_env "\$SESSION_ID"')
                self.assertRegex(text, r"(_source_lib agent-state|agent-state\.sh)")


if __name__ == "__main__":
    unittest.main()

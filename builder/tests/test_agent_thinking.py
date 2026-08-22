"""How hard each subsystem thinks, switchable at runtime.

`openclaw agent --thinking <level>` is a per-run override, so the reviewer can
reason hard while the tester does not. Without it the only control is
`thinkingDefault` in the ConfigMap: one global value, needing a redeploy.

The refusal is the important part. openclaw rejects an unknown level and the
whole turn dies, so a bad value must never reach the flag — a run that thinks
at the default is recoverable, a run that cannot start is not.
"""

import os
import unittest

from harness import ShellTestCase


class Defaults(ShellTestCase):
    def test_every_key_starts_on_the_config_default(self):
        for key in ("solver", "planning", "tester", "reviewer"):
            rc, out, _ = self.run_unit(["agent-thinking", "get", key])
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "default", key)

    def test_nothing_set_means_the_library_returns_empty(self):
        # Empty is the "pass no --thinking" signal, i.e. exactly the behaviour
        # before this feature existed.
        rc, out, _ = self.sh(
            ". $PWD/bin/agent-thinking.sh && agent_thinking solver && echo END")
        self.assertEqual(out.strip(), "END")

    def test_list_explains_that_levels_differ_by_provider(self):
        # Five levels do not exist everywhere, and a list that implied they
        # did would be the misleading part.
        rc, out, _ = self.run_unit(["agent-thinking", "list"])
        self.assertIn("kimi", out)
        self.assertIn("minimax", out)
        self.assertIn("ON or OFF", out)


class Setting(ShellTestCase):
    def test_set_and_reset_round_trip(self):
        self.run_unit(["agent-thinking", "set", "reviewer", "high"])
        _, out, _ = self.run_unit(["agent-thinking", "get", "reviewer"])
        self.assertEqual(out.strip(), "high")
        self.run_unit(["agent-thinking", "reset", "reviewer"])
        _, out, _ = self.run_unit(["agent-thinking", "get", "reviewer"])
        self.assertEqual(out.strip(), "default")

    def test_every_accepted_level(self):
        for level in ("off", "minimal", "low", "medium", "high"):
            with self.subTest(level=level):
                rc, _, err = self.run_unit(["agent-thinking", "set", "solver", level])
                self.assertEqual(rc, 0, err)
                _, out, _ = self.run_unit(["agent-thinking", "get", "solver"])
                self.assertEqual(out.strip(), level)

    def test_case_is_not_significant(self):
        self.run_unit(["agent-thinking", "set", "solver", "HIGH"])
        _, out, _ = self.run_unit(["agent-thinking", "get", "solver"])
        self.assertEqual(out.strip(), "high")

    def test_one_key_at_a_time(self):
        self.run_unit(["agent-thinking", "set", "tester", "off"])
        _, out, _ = self.run_unit(["agent-thinking", "get", "solver"])
        self.assertEqual(out.strip(), "default")

    def test_set_all(self):
        self.run_unit(["agent-thinking", "set", "all", "low"])
        for key in ("solver", "planning", "tester", "reviewer"):
            _, out, _ = self.run_unit(["agent-thinking", "get", key])
            self.assertEqual(out.strip(), "low", key)

    def test_switching_off_says_what_that_means(self):
        rc, out, _ = self.run_unit(["agent-thinking", "set", "tester", "off"])
        self.assertIn("disables reasoning", out)


class Refusals(ShellTestCase):
    def test_an_unknown_level_is_refused(self):
        rc, _, err = self.run_unit(["agent-thinking", "set", "solver", "maximum"])
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown level", err)

    def test_the_refusal_explains_the_consequence(self):
        _, _, err = self.run_unit(["agent-thinking", "set", "solver", "turbo"])
        self.assertIn("the whole turn dies", err)

    def test_a_refused_set_changes_nothing(self):
        self.run_unit(["agent-thinking", "set", "solver", "high"])
        self.run_unit(["agent-thinking", "set", "solver", "turbo"])
        _, out, _ = self.run_unit(["agent-thinking", "get", "solver"])
        self.assertEqual(out.strip(), "high")

    def test_an_unknown_key_is_refused(self):
        rc, _, err = self.run_unit(["agent-thinking", "set", "nosuch", "high"])
        self.assertNotEqual(rc, 0)


class FailSoftReads(ShellTestCase):
    """A broken store must degrade to the default, never to a bad flag."""

    def _read(self):
        rc, out, _ = self.sh(
            ". $PWD/bin/agent-thinking.sh && agent_thinking solver")
        return out.strip()

    def _write_store(self, text):
        with open(f"{self.home}/.openclaw/agent-thinking.conf", "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(text)

    def test_garbage_is_ignored_rather_than_passed_through(self):
        for junk in ("turbo", "9", "", "high low", "$(id)", "--verbose"):
            with self.subTest(junk=junk):
                self._write_store(f"solver = {junk}\n")
                self.assertEqual(self._read(), "")

    def test_a_valid_level_is_honoured(self):
        self._write_store("solver = medium\n")
        self.assertEqual(self._read(), "medium")

    def test_a_comment_is_stripped(self):
        self._write_store("solver = high # for the hard ones\n")
        self.assertEqual(self._read(), "high")

    def test_a_missing_store_is_not_an_error(self):
        self.assertEqual(self._read(), "")


class TheFlagReachesOpenclaw(ShellTestCase):
    """What the runners actually build, against the fake binary."""

    def _invoke(self):
        return self.sh(
            ". $PWD/bin/agent-thinking.sh\n"
            'AGENT_THINKING="$(agent_thinking solver)"\n'
            "ARGS=()\n"
            '[ -z "$AGENT_THINKING" ] || ARGS+=(--thinking "$AGENT_THINKING")\n'
            'openclaw agent --local "${ARGS[@]}" --timeout 60 '
            "--session-id s --message hi >/dev/null 2>&1\n"
            "echo done")

    def test_nothing_configured_sends_no_flag(self):
        self._invoke()
        argv = self.openclaw_calls()[-1]["argv"]
        self.assertNotIn("--thinking", argv)

    def test_a_configured_level_is_passed_through(self):
        self.run_unit(["agent-thinking", "set", "solver", "high"])
        self._invoke()
        argv = self.openclaw_calls()[-1]["argv"]
        self.assertIn("--thinking high", argv)

    def test_off_is_passed_through_as_a_real_level(self):
        # `off` is a decision, not an absence — it must reach openclaw rather
        # than silently becoming "inherit the default".
        self.run_unit(["agent-thinking", "set", "solver", "off"])
        self._invoke()
        self.assertIn("--thinking off", self.openclaw_calls()[-1]["argv"])


if __name__ == "__main__":
    unittest.main()

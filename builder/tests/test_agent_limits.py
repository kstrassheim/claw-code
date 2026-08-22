"""agent-limits: how long each subsystem may run, changed from chat.

The refusals matter more than the happy path: a cap under a minute kills every
agent before it can work, and one over a day outlives the stale-lock TTL, so a
wedged runner would hold its repo for a day.
"""

import unittest

from harness import ShellTestCase


class Reading(ShellTestCase):
    def test_defaults_when_nothing_is_set(self):
        rc, out, _ = self.run_unit(["agent-limits", "get", "solver.turn"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "3500")

    def test_list_shows_every_key(self):
        rc, out, _ = self.run_unit(["agent-limits", "list"])
        for key in ("solver.turn", "solver.lifetime", "tester.run",
                    "reviewer.run"):
            self.assertIn(key, out)


class Durations(ShellTestCase):
    def test_accepts_the_forms_a_human_types(self):
        for value, want in (("3600", "3600"), ("90s", "90"), ("45m", "2700"),
                            ("3h", "10800"), ("1h30m", "5400")):
            with self.subTest(value=value):
                self.run_unit(["agent-limits", "set", "solver.turn", value])
                _, out, _ = self.run_unit(["agent-limits", "get", "solver.turn"])
                self.assertEqual(out.strip(), want)

    def test_reset_returns_to_the_default(self):
        self.run_unit(["agent-limits", "set", "solver.turn", "3h"])
        self.run_unit(["agent-limits", "reset", "solver.turn"])
        _, out, _ = self.run_unit(["agent-limits", "get", "solver.turn"])
        self.assertEqual(out.strip(), "3500")

    def test_one_key_at_a_time(self):
        self.run_unit(["agent-limits", "set", "solver.turn", "3h"])
        _, out, _ = self.run_unit(["agent-limits", "get", "tester.run"])
        self.assertEqual(out.strip(), "3500")


class Refusals(ShellTestCase):
    def test_refuses_a_cap_under_a_minute(self):
        rc, _, err = self.run_unit(["agent-limits", "set", "solver.turn", "5s"])
        self.assertNotEqual(rc, 0)
        self.assertIn("out of range", err)

    def test_refuses_a_cap_over_a_day(self):
        rc, _, err = self.run_unit(["agent-limits", "set", "solver.turn", "48h"])
        self.assertNotEqual(rc, 0)

    def test_refuses_a_duration_that_does_not_parse(self):
        rc, _, err = self.run_unit(["agent-limits", "set", "solver.turn", "soon"])
        self.assertNotEqual(rc, 0)
        self.assertIn("not a duration", err)

    def test_refuses_an_unknown_key(self):
        rc, _, err = self.run_unit(["agent-limits", "set", "nosuch", "1h"])
        self.assertNotEqual(rc, 0)

    def test_a_refused_set_changes_nothing(self):
        self.run_unit(["agent-limits", "set", "solver.turn", "5s"])
        _, out, _ = self.run_unit(["agent-limits", "get", "solver.turn"])
        self.assertEqual(out.strip(), "3500")


class Settings(ShellTestCase):
    """The non-duration knobs, which share the store but not the parser.

    Reading a story-point threshold through the duration path would reject 3 as
    "under a minute" and silently hand back the default, so the setting would
    look changeable and not be.
    """

    def test_the_cheap_lane_threshold_round_trips(self):
        self.run_unit(["agent-limits", "set", "solver.small.max_points", "5"])
        _, out, _ = self.run_unit(["agent-limits", "get",
                                   "solver.small.max_points"])
        self.assertEqual(out.strip(), "5")

    def test_a_disabled_threshold_reads_off_not_zero(self):
        # The chat skills answer "what is the split limit" from this output,
        # and 0 reads as a number of points — the one thing it does not mean.
        self.run_unit(["agent-limits", "set", "planning.split_points", "off"])
        _, out, _ = self.run_unit(["agent-limits", "get",
                                   "planning.split_points"])
        self.assertEqual(out.strip(), "off")

    def test_a_flag_round_trips(self):
        self.run_unit(["agent-limits", "set", "solver.autoruntime", "on"])
        _, out, _ = self.run_unit(["agent-limits", "get", "solver.autoruntime"])
        self.assertEqual(out.strip(), "on")

    def test_a_threshold_refuses_a_non_number(self):
        rc, _, err = self.run_unit(
            ["agent-limits", "set", "solver.small.max_points", "3h"])
        self.assertNotEqual(rc, 0)
        self.assertIn("not a whole number", err)

    def test_a_flag_refuses_anything_but_on_or_off(self):
        rc, _, err = self.run_unit(
            ["agent-limits", "set", "solver.autoruntime", "sometimes"])
        self.assertNotEqual(rc, 0)
        self.assertIn("not on or off", err)

    def test_resetting_a_setting_prints_its_default_not_an_arithmetic_error(self):
        # A setting has no duration default, so rendering it through the
        # seconds formatter printed a shell arithmetic error and then the word
        # "default" with nothing after it — and the chat skills read that line
        # back to the operator as the answer to "reset it".
        self.run_unit(["agent-limits", "set", "solver.small.max_points", "5"])
        rc, out, err = self.run_unit(
            ["agent-limits", "reset", "solver.small.max_points"])
        self.assertEqual(rc, 0, err)
        self.assertIn("back to default 3", out)
        self.assertNotIn("error", err.lower())


class FailSoft(ShellTestCase):
    """A misconfigured limit must not stop a subsystem — it must run with the
    built-in value and leave the operator to notice."""

    def _read(self):
        rc, out, _ = self.sh(
            '. $PWD/bin/agent-limits.sh && agent_limit solver.turn 3500')
        return out.strip()

    def test_garbage_in_the_store_falls_back(self):
        for junk in ("abc", "-5", "0", "", "3h4x", "999999999"):
            with self.subTest(junk=junk):
                with open(f"{self.home}/.openclaw/agent-limits.conf", "w",
                          encoding="utf-8", newline="\n") as f:
                    f.write(f"solver.turn = {junk}\n")
                self.assertEqual(self._read(), "3500")

    def test_a_missing_file_falls_back(self):
        self.assertEqual(self._read(), "3500")

    def test_a_valid_value_is_honoured(self):
        with open(f"{self.home}/.openclaw/agent-limits.conf", "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("solver.turn = 7200\n")
        self.assertEqual(self._read(), "7200")


class WhetherAPersonSetIt(ShellTestCase):
    """`agent_limit_is_set` — "nobody set this" versus "somebody set it".

    `agent_limit` cannot answer that: it returns the default when the key is
    absent, so a key set to exactly the default is indistinguishable from one
    that was never touched. Anywhere a value can also be DERIVED, the two lead
    to opposite decisions — a derived number may replace a built-in default,
    and must not replace a human who deliberately configured one.
    """

    def _write(self, text):
        with open(f"{self.home}/.openclaw/agent-limits.conf", "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(text)

    def _is_set(self, key="solver.lifetime"):
        rc, _, _ = self.sh(
            f'. $PWD/bin/agent-limits.sh && agent_limit_is_set {key}')
        return rc == 0

    def test_a_configured_value_reads_as_set(self):
        self._write("solver.lifetime = 4h\n")
        self.assertTrue(self._is_set())

    def test_an_untouched_key_reads_as_unset(self):
        self._write("solver.turn = 3h\n")
        self.assertFalse(self._is_set())

    def test_a_missing_store_reads_as_unset(self):
        self.assertFalse(self._is_set())

    def test_a_value_set_to_exactly_the_default_still_reads_as_set(self):
        # The case `agent_limit` cannot distinguish, and the reason this
        # exists. Somebody typed it; that is a decision.
        self._write("solver.turn = 3500\n")
        self.assertTrue(self._is_set("solver.turn"))

    def test_an_unusable_value_reads_as_unset(self):
        # A typo already falls back to the default inside `agent_limit`.
        # Reporting it as configured would let it suppress a derived value and
        # leave the run on a number nobody chose — the worst of both.
        for junk in ("eventually", "0", "-5", "3h4x", "999999999"):
            with self.subTest(junk=junk):
                self._write(f"solver.lifetime = {junk}\n")
                self.assertFalse(self._is_set())


if __name__ == "__main__":
    unittest.main()

"""agent-models: which LLM each subsystem runs on.

Shell, so these run the real CLI against a fake `openclaw` and a throwaway
HOME. The value is in the refusals: an unresolvable model id would kill every
run the subsystem starts, and the planner would respawn it forever.
"""

import os
import unittest

from harness import ShellTestCase


class Defaults(ShellTestCase):
    def setUp(self):
        super().setUp()
        self.baseline = os.path.join(self.home, ".openclaw", "runner-model.default")
        with open(self.baseline, "w", encoding="utf-8") as f:
            f.write("kimi/k3")

    def test_every_key_starts_on_the_baseline(self):
        for key in ("solver", "planning", "tester", "reviewer"):
            rc, out, _ = self.run_unit(["agent-models", "get", key])
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "kimi/k3", key)

    def test_get_returns_the_effective_model_not_the_word_default(self):
        # Callers asking "which model does the tester run on" want the answer.
        rc, out, _ = self.run_unit(["agent-models", "get", "tester"])
        self.assertNotIn("default", out)

    def test_a_missing_baseline_degrades_to_inheriting(self):
        os.unlink(self.baseline)
        rc, out, _ = self.run_unit(["agent-models", "get", "solver"])
        self.assertEqual(rc, 0)
        # Empty means "pass no --model", i.e. exactly the behaviour before the
        # feature existed. It must not be an error.
        self.assertIn("inherited", out)


class Setting(ShellTestCase):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.home, ".openclaw", "runner-model.default"),
                  "w", encoding="utf-8") as f:
            f.write("kimi/k3")

    def test_set_and_reset_round_trip(self):
        self.run_unit(["agent-models", "set", "solver", "minimax/MiniMax-M3"])
        _, out, _ = self.run_unit(["agent-models", "get", "solver"])
        self.assertEqual(out.strip(), "minimax/MiniMax-M3")
        self.run_unit(["agent-models", "reset", "solver"])
        _, out, _ = self.run_unit(["agent-models", "get", "solver"])
        self.assertEqual(out.strip(), "kimi/k3")

    def test_setting_one_key_leaves_the_others_alone(self):
        self.run_unit(["agent-models", "set", "solver", "minimax/MiniMax-M3"])
        _, out, _ = self.run_unit(["agent-models", "get", "reviewer"])
        self.assertEqual(out.strip(), "kimi/k3")

    def test_planning_switches_independently(self):
        # The point of the separate key: planning must keep working on another
        # provider when the Kimi quota is spent, without moving the runners.
        self.run_unit(["agent-models", "set", "planning", "minimax/MiniMax-M3"])
        for other in ("solver", "tester", "reviewer"):
            _, out, _ = self.run_unit(["agent-models", "get", other])
            self.assertEqual(out.strip(), "kimi/k3", other)

    def test_set_all(self):
        self.run_unit(["agent-models", "set", "all", "minimax/MiniMax-M3"])
        for key in ("solver", "planning", "tester", "reviewer"):
            _, out, _ = self.run_unit(["agent-models", "get", key])
            self.assertEqual(out.strip(), "minimax/MiniMax-M3", key)


class Refusals(ShellTestCase):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.home, ".openclaw", "runner-model.default"),
                  "w", encoding="utf-8") as f:
            f.write("kimi/k3")

    def test_refuses_a_model_that_does_not_exist(self):
        rc, _, err = self.run_unit(["agent-models", "set", "solver", "gpt-9/turbo"])
        self.assertNotEqual(rc, 0)
        self.assertIn("not a usable model", err)

    def test_refuses_a_model_with_no_credentials(self):
        # Present in the listing but without the `configured` tag: a run on it
        # would fail at the first request.
        rc, _, err = self.run_unit(
            ["agent-models", "set", "solver", "minimax-cn/MiniMax-M3"])
        self.assertNotEqual(rc, 0)
        self.assertIn("no credentials", err)

    def test_a_refused_set_changes_nothing(self):
        self.run_unit(["agent-models", "set", "solver", "gpt-9/turbo"])
        _, out, _ = self.run_unit(["agent-models", "get", "solver"])
        self.assertEqual(out.strip(), "kimi/k3")

    def test_refuses_an_unknown_key(self):
        rc, _, err = self.run_unit(["agent-models", "set", "nosuch", "kimi/k3"])
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown key", err)

    def test_the_refusal_says_what_IS_usable(self):
        _, _, err = self.run_unit(["agent-models", "set", "solver", "gpt-9/turbo"])
        self.assertIn("minimax/MiniMax-M3", err)


class TheCheapLaneKeysReportOffWhenUnset(ShellTestCase):
    """`solver.small` / `reviewer.small` are opt-in, and `list` is where an
    operator checks whether they are on.

    Reporting an unset lane key as the baseline says the opposite of the truth
    — that small work is routed somewhere, when in fact nothing is. That is the
    failure this class pins down: the solver's half set, the reviewer's never
    set, and nothing in the output making the difference visible.
    """

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.home, ".openclaw", "runner-model.default"),
                  "w", encoding="utf-8") as f:
            f.write("kimi/k3")

    def test_an_unset_lane_key_reads_off_not_the_baseline(self):
        for key in ("solver.small", "reviewer.small"):
            with self.subTest(key=key):
                _, out, _ = self.run_unit(["agent-models", "get", key])
                self.assertIn("off", out)
                self.assertNotIn("kimi/k3", out)

    def test_an_ordinary_key_still_reports_the_baseline(self):
        _, out, _ = self.run_unit(["agent-models", "get", "reviewer"])
        self.assertEqual(out.strip(), "kimi/k3")

    def test_setting_it_turns_the_lane_on_and_says_so(self):
        self.run_unit(["agent-models", "set", "reviewer.small",
                       "minimax/MiniMax-M3"])
        _, out, _ = self.run_unit(["agent-models", "get", "reviewer.small"])
        self.assertEqual(out.strip(), "minimax/MiniMax-M3")

    def test_resetting_it_turns_the_lane_back_off(self):
        self.run_unit(["agent-models", "set", "reviewer.small",
                       "minimax/MiniMax-M3"])
        self.run_unit(["agent-models", "reset", "reviewer.small"])
        _, out, _ = self.run_unit(["agent-models", "get", "reviewer.small"])
        self.assertIn("off", out)

    def test_the_reviewer_half_is_set_independently_of_the_solver_half(self):
        # Setting solver.small alone is the trap: small stories written on the
        # cheap model and still reviewed on the expensive one, with the review
        # half silently never routed.
        self.run_unit(["agent-models", "set", "solver.small",
                       "minimax/MiniMax-M3"])
        _, out, _ = self.run_unit(["agent-models", "get", "reviewer.small"])
        self.assertIn("off", out)

    def test_list_shows_both_halves(self):
        _, out, _ = self.run_unit(["agent-models", "list"])
        self.assertIn("solver.small", out)
        self.assertIn("reviewer.small", out)
        self.assertIn("off (not set)", out)

    def test_an_unusable_model_is_refused_here_too(self):
        rc, _, err = self.run_unit(
            ["agent-models", "set", "reviewer.small", "gpt-9/turbo"])
        self.assertNotEqual(rc, 0)
        self.assertIn("not a usable model", err)

    def test_is_lane_key_separates_the_opt_in_keys_from_the_rest(self):
        # The one rule behind every assertion above: a `.small` key reports
        # off-when-unset, an ordinary key reports the baseline. Getting this
        # backwards for `reviewer` would hide the baseline behind "off"; for
        # `reviewer.small` it would restore the misreport in the first place.
        src = "cd $HOME/bin && . ./agent-models list >/dev/null 2>&1"
        for key, lane in (("solver.small", True), ("reviewer.small", True),
                          ("solver", False), ("reviewer", False),
                          ("planning", False), ("tester", False)):
            with self.subTest(key=key):
                rc, out, err = self.sh(
                    f"{src}; _is_lane_key {key} && echo yes || echo no")
                self.assertEqual(rc, 0, err)
                self.assertEqual(out.strip(), "yes" if lane else "no")


class FailSoftReads(ShellTestCase):
    """The library path must never stop a run — only degrade it."""

    def setUp(self):
        super().setUp()
        self.baseline = os.path.join(self.home, ".openclaw", "runner-model.default")
        with open(self.baseline, "w", encoding="utf-8") as f:
            f.write("kimi/k3")
        self.store = os.path.join(self.home, ".openclaw", "agent-models.conf")

    def _read(self):
        rc, out, _ = self.sh(
            ". $HOME/bin/agent-models.sh && agent_model solver")
        return out.strip()

    def test_garbage_falls_back_to_the_baseline(self):
        for junk in ("not a model", "a/b/c", "   ", "rm -rf /", "kimi k3",
                     "$(whoami)", "kimi/k3;id"):
            with self.subTest(junk=junk):
                with open(self.store, "w", encoding="utf-8") as f:
                    f.write(f"solver = {junk}\n")
                self.assertEqual(self._read(), "kimi/k3")

    def test_inner_whitespace_is_not_squeezed_away(self):
        # Regression: stripping ALL whitespace folded "rm -rf /" into "rm-rf/",
        # which then satisfied the provider/model shape and would have been
        # passed to --model as a real-looking id.
        with open(self.store, "w", encoding="utf-8") as f:
            f.write("solver = rm -rf /\n")
        self.assertEqual(self._read(), "kimi/k3")

    def test_a_comment_is_stripped_but_the_value_survives(self):
        with open(self.store, "w", encoding="utf-8") as f:
            f.write("solver = kimi/k3 # chosen on purpose\n")
        self.assertEqual(self._read(), "kimi/k3")

    def test_a_missing_store_is_not_an_error(self):
        self.assertEqual(self._read(), "kimi/k3")

    def test_an_unset_lane_key_reads_empty_not_the_baseline(self):
        # agent_model_raw is what the runners ask, because agent_model answers
        # with the baseline and is therefore almost never empty — routing every
        # small story somewhere merely because a baseline exists.
        rc, out, _ = self.sh(
            ". $HOME/bin/agent-models.sh && "
            "agent_model_raw solver.small && echo END")
        self.assertEqual(out.strip(), "END")


class EverySubcommandRuns(ShellTestCase):
    """`available` once shipped broken: a helper was renamed and this call
    site was missed. Nothing exercised it, so it surfaced in production."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.home, ".openclaw", "runner-model.default"),
                  "w", encoding="utf-8") as f:
            f.write("kimi/k3")

    def test_no_subcommand_hits_an_undefined_function(self):
        for argv in (["list"], ["available"], ["get", "solver"],
                     ["set", "solver", "kimi/k3"], ["reset", "solver"],
                     ["reset", "all"], ["help"]):
            with self.subTest(argv=argv):
                _, out, err = self.run_unit(["agent-models"] + argv)
                self.assertNotIn("not found", out + err)


if __name__ == "__main__":
    unittest.main()

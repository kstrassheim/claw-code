"""How long a solve may take, taken from the size somebody estimated.

WHY THE ESTIMATE SHOULD SET THE BUDGET
--------------------------------------
The point scale is calibrated in model time — roughly a quarter of an hour per
point — which means a sized story has already been told how long it ought to
take. Giving every story the same flat lifetime ignores that in the expensive
direction: a one-point story that wedges takes the full six hours to admit it,
and the repository whose lock it holds waits all six.

THE THREE PROPERTIES THAT MAKE IT SAFE TO HAVE ON BY DEFAULT

  * It only ever fires for a story a person actually sized. An unestimated
    story is treated as 8 points so it gets the strong model and the large
    budget — but that 8 is the bot's own invention, and cutting a run short on
    the strength of an invented number is how work stops halfway.
  * A configured cap wins over the derived one. With this on by default,
    silently overriding `agent-limits set solver.lifetime` would make that
    command look broken to the person who just used it.
  * Whichever number won is LOGGED. Two sources for one budget that disagree
    in silence are worse than either of them alone.

The block is extracted from the runner rather than copied, so a change to the
real lines is a change to what these tests exercise.
"""

import os
import re
import unittest

from harness import BUILDER, ShellTestCase

RUNNER = "fixer-runner.sh"
START = "# The story's SIZE, resolved once, here"
END = "# How hard this subsystem thinks."


class AutoRuntime(ShellTestCase):
    def block(self):
        import shutil
        src = self.extract_block(RUNNER, START, END)
        dst = os.path.join(self.home, "limits.sh")
        shutil.move(src, dst)
        return "limits.sh"

    def run_block(self, *, points="", conf=None):
        """(lifetime, turn, output) for a run of the real block."""
        conf_path = os.path.join(self.home, "agent-limits.conf")
        with open(conf_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(conf or "")
        script = "\n".join([
            "set -u",
            "REPO=o/r", "ISSUE_NUM=5",
            'export AGENT_LIMITS_FILE="$PWD/agent-limits.conf"',
            f'STORY_POINTS="{points}"',
            '. "$PWD/../agent-limits.sh" 2>/dev/null || . /usr/local/bin/agent-limits.sh',
            f'. "$PWD/{self.block()}"',
            'echo "LIFETIME=$MAX_LIFETIME_SECONDS"',
            'echo "TURN=$AGENT_TURN_TIMEOUT"',
        ])
        # The library lives beside the runner, not in the sandbox.
        script = script.replace('"$PWD/../agent-limits.sh"',
                                f'"{os.path.join(BUILDER, "agent-limits.sh")}"')
        rc, out, err = self.sh(script)
        self.assertEqual(rc, 0, f"block failed: {err or out}")
        lifetime = int(re.search(r"LIFETIME=(\d+)", out).group(1))
        turn = int(re.search(r"TURN=(\d+)", out).group(1))
        return lifetime, turn, out

    # -- the derivation ---------------------------------------------------

    def test_a_small_story_gets_a_small_budget(self):
        # The whole point: a one-pointer that wedges says so in fifteen
        # minutes rather than in six hours.
        lifetime, turn, out = self.run_block(points="1")
        self.assertEqual(lifetime, 900)
        self.assertEqual(turn, 450)
        self.assertIn("auto runtime", out)

    def test_the_budget_scales_with_the_estimate(self):
        for points, expected in (("2", 1800), ("5", 4500), ("8", 7200),
                                 ("13", 11700)):
            with self.subTest(points=points):
                lifetime, _, _ = self.run_block(points=points)
                self.assertEqual(lifetime, expected)

    def test_a_turn_never_gets_the_whole_run(self):
        # A run has to be able to react to its own output. A per-turn cap
        # equal to the lifetime lets the first turn eat everything and leaves
        # nothing to act on the result with.
        for points in ("1", "3", "8", "13"):
            with self.subTest(points=points):
                lifetime, turn, _ = self.run_block(points=points)
                self.assertLess(turn, lifetime)

    # -- what it refuses to do --------------------------------------------

    def test_an_unestimated_story_keeps_the_configured_caps(self):
        # An unestimated story is worked as an 8, and that 8 is the bot's own
        # invention. Deriving a budget from it would cut a run short on the
        # strength of a number nobody produced.
        lifetime, turn, out = self.run_block(points="")
        self.assertEqual(lifetime, 6 * 3600)
        self.assertEqual(turn, 3500)
        self.assertIn("unestimated", out)

    def test_a_nonsense_size_is_treated_as_unestimated(self):
        lifetime, _, _ = self.run_block(points="large")
        self.assertEqual(lifetime, 6 * 3600)

    def test_a_configured_lifetime_beats_the_derived_one(self):
        # Someone typed this into chat about this deployment. Overriding it
        # silently is what would make `agent-limits set` look broken.
        lifetime, _, out = self.run_block(points="1",
                                          conf="solver.lifetime = 4h\n")
        self.assertEqual(lifetime, 4 * 3600)
        self.assertIn("keeping the configured solver.lifetime", out)

    def test_a_configured_turn_beats_the_derived_one(self):
        _, turn, out = self.run_block(points="1", conf="solver.turn = 20m\n")
        self.assertEqual(turn, 1200)
        self.assertIn("keeping the configured solver.turn", out)

    def test_one_configured_cap_does_not_suppress_the_other(self):
        # Setting a lifetime is not a statement about per-turn time. Letting
        # it disable the whole derivation would make the two settings a single
        # switch that nobody named.
        lifetime, turn, _ = self.run_block(points="2",
                                           conf="solver.lifetime = 4h\n")
        self.assertEqual(lifetime, 4 * 3600)
        self.assertEqual(turn, 900)

    def test_an_unusable_configured_value_does_not_count_as_configured(self):
        # A typo falls back to the default inside agent_limit. Treating it as
        # "configured" would let it suppress the derived number and leave the
        # run on a value nobody chose.
        lifetime, _, _ = self.run_block(points="2",
                                        conf="solver.lifetime = eventually\n")
        self.assertEqual(lifetime, 1800)

    # -- the switch --------------------------------------------------------

    def test_it_is_on_without_anybody_configuring_anything(self):
        lifetime, _, _ = self.run_block(points="2", conf="")
        self.assertEqual(lifetime, 1800)

    def test_switching_it_off_restores_the_flat_caps(self):
        lifetime, turn, out = self.run_block(
            points="1", conf="solver.autoruntime = off\n")
        self.assertEqual(lifetime, 6 * 3600)
        self.assertEqual(turn, 3500)
        self.assertNotIn("auto runtime", out)

    def test_the_setting_is_listed_so_it_can_be_switched(self):
        # A behaviour with no listed setting cannot be turned off by anybody
        # who did not read the runner.
        with open(os.path.join(BUILDER, "agent-limits"), encoding="utf-8") as f:
            registry = f.read()
        self.assertIn("solver.autoruntime|flag|on|", registry)


class TheSizeIsResolvedOnce(ShellTestCase):
    """$STORY_POINTS is left alone; $SOLVER_POINTS is the working value."""

    def test_the_raw_estimate_is_not_overwritten_by_the_default(self):
        # The planning record is written from $STORY_POINTS further down the
        # runner. If the default were written back into it, an unestimated
        # story would be recorded as an 8-point estimate — a number nobody
        # produced, in the store the reports are built from.
        import shutil
        src = self.extract_block(RUNNER, START, END)
        dst = os.path.join(self.home, "limits.sh")
        shutil.move(src, dst)
        with open(os.path.join(self.home, "agent-limits.conf"), "w") as f:
            f.write("")
        rc, out, err = self.sh("\n".join([
            "set -u", "REPO=o/r", "ISSUE_NUM=5",
            'export AGENT_LIMITS_FILE="$PWD/agent-limits.conf"',
            'STORY_POINTS=""',
            f'. "{os.path.join(BUILDER, "agent-limits.sh")}"',
            '. "$PWD/limits.sh"',
            'echo "RAW=[${STORY_POINTS}]"',
            'echo "WORKING=$SOLVER_POINTS"',
            'echo "DEFAULTED=$POINTS_DEFAULTED"',
        ]))
        self.assertEqual(rc, 0, err or out)
        self.assertIn("RAW=[]", out)
        self.assertIn("WORKING=8", out)
        self.assertIn("DEFAULTED=1", out)


if __name__ == "__main__":
    unittest.main()

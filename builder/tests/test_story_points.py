"""Story points measured in the unit that actually costs money: model calls.

The bands are the contract between three things that must not disagree — the
estimator that sizes a story, the router that picks a model and a time budget
for it, and the report that later says whether the estimate was any good. Every
test here is about a BOUNDARY, because a band that is off by one at its edge is
invisible in the middle and wrong exactly where the decision is close.

Rounding direction gets its own attention. Under-estimating is the expensive
direction: it is what makes a run die half-finished, having spent the tokens.
"""

import os
import shutil
import tempfile
import unittest

from harness import TMP_ROOT, load, temp_env


class BandBoundaries(unittest.TestCase):
    def setUp(self):
        self.sp = load("story_points")

    def test_each_band_includes_its_upper_bound(self):
        # Inclusive upper, exclusive lower. A story that cost exactly 60 calls
        # is a 3, not a 5.
        for points, upper in self.sp.BANDS:
            with self.subTest(points=points):
                self.assertEqual(self.sp.points_for_calls(upper), points)

    def test_one_call_past_a_bound_is_the_next_size_up(self):
        for points, upper in self.sp.BANDS:
            if points == self.sp.BANDS[-1][0]:
                continue
            with self.subTest(points=points):
                self.assertGreater(self.sp.points_for_calls(upper + 1), points)

    def test_no_calls_at_all_is_the_smallest_size(self):
        self.assertEqual(self.sp.points_for_calls(0), self.sp.SCALE[0])

    def test_past_the_top_band_is_too_big(self):
        beyond = self.sp.BANDS[-1][1] + 1
        self.assertEqual(self.sp.points_for_calls(beyond), self.sp.SPLIT_POINTS)

    def test_an_unmeasurable_cost_defaults_high(self):
        # None and negative both mean "we do not know". Defaulting LOW would
        # route the work to a smaller budget on no evidence.
        for value in (None, -1):
            with self.subTest(value=value):
                self.assertEqual(self.sp.points_for_calls(value),
                                 self.sp.DEFAULT_POINTS)

    def test_the_band_of_a_size_is_the_inverse_of_the_lookup(self):
        # The two directions are used by different callers — the estimator
        # reads calls_band, the report reads points_for_calls — and they must
        # describe the same partition.
        for points, upper in self.sp.BANDS:
            with self.subTest(points=points):
                lower, hi = self.sp.calls_band(points)
                self.assertEqual(hi, upper)
                self.assertEqual(self.sp.points_for_calls(lower + 1), points)

    def test_the_bands_are_contiguous_with_no_gap_or_overlap(self):
        # A gap would leave a call count with no size at all; an overlap would
        # make the answer depend on iteration order.
        previous = 0
        for _, upper in self.sp.BANDS:
            self.assertGreater(upper, previous)
            previous = upper

    def test_the_top_band_is_unbounded_above(self):
        self.assertEqual(self.sp.calls_band(self.sp.SPLIT_POINTS)[1], -1)


class CoercingWhatSomebodyWroteOnAnIssue(unittest.TestCase):
    def setUp(self):
        self.sp = load("story_points")

    def test_an_off_scale_weight_rounds_UP(self):
        # A story someone sized at 4 is closer in risk to a 5 than to a 3, and
        # rounding down is the direction that starves a run.
        self.assertEqual(self.sp.normalise(4), 5)
        self.assertEqual(self.sp.normalise(6), 8)

    def test_a_value_on_the_scale_is_unchanged(self):
        for value in self.sp.SCALE:
            with self.subTest(value=value):
                self.assertEqual(self.sp.normalise(value), value)

    def test_a_label_written_as_a_string_still_works(self):
        self.assertEqual(self.sp.normalise("3"), 3)
        self.assertEqual(self.sp.normalise("  5 "), 5)

    def test_nonsense_is_no_estimate_rather_than_a_number(self):
        for junk in (None, "", "large", "3 points", 0, -2):
            with self.subTest(junk=junk):
                self.assertIsNone(self.sp.normalise(junk))

    def test_an_unestimated_story_is_planned_high_and_says_it_was_a_guess(self):
        # `defaulted` has to travel with the number: a sprint whose added
        # scope is all defaulted 8s is not the same sprint as one estimated,
        # and from the number alone nobody can tell them apart afterwards.
        points, defaulted = self.sp.effective(None)
        self.assertEqual(points, self.sp.DEFAULT_POINTS)
        self.assertTrue(defaulted)

    def test_a_judged_estimate_is_not_marked_as_a_guess(self):
        points, defaulted = self.sp.effective(3)
        self.assertEqual(points, 3)
        self.assertFalse(defaulted)

    def test_anything_at_or_above_the_ceiling_is_too_big(self):
        self.assertTrue(self.sp.is_too_big(self.sp.SPLIT_POINTS))
        self.assertTrue(self.sp.is_too_big(100))
        self.assertFalse(self.sp.is_too_big(self.sp.SCALE[-1]))

    def test_no_estimate_is_not_too_big(self):
        # "Unknown" must not be routed as "must be split", or every issue that
        # arrives without a label is parked instead of started.
        self.assertFalse(self.sp.is_too_big(None))


class TheFeedbackLoop(unittest.TestCase):
    def setUp(self):
        self.sp = load("story_points")

    def test_drift_is_positive_when_the_work_cost_more(self):
        est, req, delta = self.sp.drift(2, 200)
        self.assertEqual(est, 2)
        self.assertGreater(delta, 0)

    def test_drift_is_zero_when_the_estimate_held(self):
        _, _, delta = self.sp.drift(3, 45)
        self.assertEqual(delta, 0)

    def test_drift_is_negative_when_the_work_was_cheaper(self):
        _, _, delta = self.sp.drift(8, 10)
        self.assertLess(delta, 0)

    def test_describe_states_the_basis_and_not_just_the_number(self):
        # It goes into an issue comment. A bare "5" tells the reader nothing
        # about what the bot expects the story to cost.
        text = self.sp.describe(5)
        self.assertIn("5 point", text)
        self.assertIn("model calls", text)

    def test_describe_says_when_there_was_no_estimate(self):
        self.assertIn("unestimated", self.sp.describe(None))

    def test_describe_asks_for_a_split_rather_than_naming_a_duration(self):
        text = self.sp.describe(self.sp.SPLIT_POINTS)
        self.assertIn("split", text)


class TheCeilingIsASetting(unittest.TestCase):
    """"Too big to start" is a measurement, not a law, so it is configurable.

    It is read from the same file the shell units read, because the estimator
    is shell and the reports are Python: if those two disagreed about what
    "too big" means, one would park a story the other happily plans and the
    numbers would stop adding up with nothing saying why.
    """

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="points-", dir=TMP_ROOT)
        self.conf = os.path.join(self.dir, "agent-limits.conf")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def points_with(self, text):
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(text)
        ctx = temp_env(AGENT_LIMITS_FILE=self.conf)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return load("story_points")

    def test_a_lower_ceiling_shortens_the_usable_scale(self):
        # The intended reading of a lower ceiling: smaller stories, more of
        # them — not merely a renamed top of the same scale.
        sp = self.points_with("planning.split_points = 5\n")
        self.assertEqual(sp.usable_scale(), (1, 2, 3))
        self.assertTrue(sp.is_too_big(5))

    def test_switching_the_ceiling_off_means_nothing_is_ever_too_big(self):
        # A real choice: attempt a large story and stop half-finished rather
        # than be told to split it.
        sp = self.points_with("planning.split_points = off\n")
        self.assertFalse(sp.SPLIT_ENABLED)
        self.assertFalse(sp.is_too_big(100))
        self.assertEqual(sp.normalise(100), sp.SCALE[-1])

    def test_a_typo_falls_back_instead_of_stopping_the_estimator(self):
        sp = self.points_with("planning.split_points = enormous\n")
        self.assertEqual(sp.SPLIT_POINTS, sp.SPLIT_DEFAULT)

    def test_a_ceiling_of_one_is_not_off_it_is_stop(self):
        # Every story would be too big and nothing would ever start again.
        sp = self.points_with("planning.split_points = 1\n")
        self.assertEqual(sp.SPLIT_POINTS, sp.SPLIT_DEFAULT)

    def test_the_scale_shown_to_the_estimator_matches_the_ceiling(self):
        # It is shown to a MODEL, which answers with a number from it. A table
        # offering 8 next to a ceiling of 5 produces estimates the wrapper then
        # refuses, and the refusal reads as the model misbehaving rather than
        # as two texts disagreeing.
        sp = self.points_with("planning.split_points = 5\n")
        table = sp.scale_table()
        self.assertIn("3 point", table)
        self.assertNotIn("8 point", table)
        self.assertIn("TOO BIG", table)

    def test_with_no_ceiling_the_table_says_the_top_size_is_the_answer(self):
        sp = self.points_with("planning.split_points = off\n")
        table = sp.scale_table()
        self.assertNotIn("TOO BIG", table)
        self.assertIn("no size above this scale", table)


if __name__ == "__main__":
    unittest.main()

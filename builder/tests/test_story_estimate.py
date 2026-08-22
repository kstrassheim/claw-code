"""The story label vocabulary: what a label means, and what a write costs.

Every test here names the decision it protects. The expensive mistakes in this
area are all silent:

  - a size the bot writes but cannot read back routes every future run of that
    story to the wrong model, and nothing in the issue looks wrong;
  - a second size label the platform is happy to hold makes "how big is this?"
    have two answers;
  - a write that fires on an unchanged issue stamps the timeline every five
    minutes until no human can read past it;
  - an approval gate that answers "yes" when it cannot read the labels stops
    every merge in every repository at once.
"""

import unittest

from harness import load


class Exploding:
    """A label list that raises when read.

    Stands in for the shapes an API can hand back on a bad day — a truncated
    response, an error object, something that is not a list at all. Reading has
    to survive it; see ApprovalFailsOpen.
    """

    def __iter__(self):
        raise RuntimeError("the label list could not be read")


class SizeReading(unittest.TestCase):
    def setUp(self):
        self.e = load("story_estimate")

    def test_the_written_spelling_reads_back(self):
        # The property everything else rests on: whatever the bot writes, the
        # next tick must read as the same number.
        self.assertEqual(self.e.points_of_labels(["SP::5"]), 5)

    def test_every_accepted_scope_is_read(self):
        # A repository that sized its issues by hand before the bot arrived did
        # not use our word for it. Refusing to see those numbers would
        # re-estimate work somebody has already estimated.
        for spelling in ("SP::3", "sp::3", "Sp::3",
                         "storypoints::3", "Story Points::3",
                         "story-points::3", "points::3", "Points::3",
                         "size::3", "Size::3", "weight::3", "Weight::3"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.e.points_of_label(spelling), 3)

    def test_an_off_scale_number_rounds_up(self):
        # Rounding down is the direction that starves a run.
        self.assertEqual(self.e.points_of_label("SP::4"), 5)
        self.assertEqual(self.e.points_of_label("weight::7"), 8)

    def test_a_bare_number_label_is_not_a_size(self):
        # Plenty of teams label issues `5`, `v5` or `Q3`. A wrong size silently
        # routes the work to the wrong model, so only scoped forms count.
        self.assertIsNone(self.e.points_of_label("5"))
        self.assertIsNone(self.e.points_of_label("v5"))
        self.assertIsNone(self.e.points_of_labels(["5", "bug", "Q3"]))

    def test_a_scoped_label_that_names_no_number_is_not_a_size(self):
        self.assertIsNone(self.e.points_of_label("size::XL"))
        self.assertIsNone(self.e.points_of_label("SP::later"))
        self.assertIsNone(self.e.points_of_label("SP::0"))

    def test_the_largest_wins_when_two_labels_disagree(self):
        # Two sizes on one issue is a mistake, not an instruction; resolving it
        # downwards starves the run that then has to do the bigger job.
        self.assertEqual(self.e.points_of_labels(["SP::2", "points::8"]), 8)

    def test_reading_accepts_the_shapes_callers_actually_hold(self):
        # The API returns dicts, the tick passes the array, logs carry strings.
        self.assertEqual(self.e.points_of([{"name": "SP::5"}]), 5)
        self.assertEqual(self.e.points_of({"labels": [{"name": "SP::5"}]}), 5)
        self.assertEqual(self.e.points_of(["SP::5"]), 5)

    def test_an_unlabelled_issue_is_unestimated(self):
        self.assertIsNone(self.e.points_of({"labels": []}))
        self.assertIsNone(self.e.points_of({}))

    def test_there_is_no_weight_field_to_fall_back_on(self):
        # GitHub issues have no size field. A `weight` key on the issue dict is
        # something an importer invented, and trusting it would create a second
        # source of truth that can disagree with the only real one.
        self.assertIsNone(self.e.points_of({"weight": 3, "labels": []}))

    def test_an_unestimated_issue_defaults_high_and_says_so(self):
        # A defaulted 8 and a judged 8 route identically but must never be
        # reported as the same fact.
        self.assertEqual(self.e.effective_points({"labels": []}), (8, True))
        self.assertEqual(self.e.effective_points({"labels": ["SP::3"]}), (3, False))

    def test_a_broken_label_list_reads_as_unestimated(self):
        # A planner that raises on one malformed issue stops planning every
        # other issue in the same tick.
        self.assertIsNone(self.e.points_of_labels(Exploding()))
        self.assertIsNone(self.e.points_of_labels(None))
        self.assertIsNone(self.e.points_of_labels(42))


class SizeWriting(unittest.TestCase):
    def setUp(self):
        self.e = load("story_estimate")

    def test_only_one_spelling_is_ever_written(self):
        self.assertEqual(self.e.label_for(5), "SP::5")
        add, _ = self.e.label_updates([], 5)
        self.assertEqual(add, ["SP::5"])

    def test_sizing_a_fresh_issue_adds_one_label(self):
        add, remove = self.e.label_updates(["bug"], 3)
        self.assertEqual(add, ["SP::3"])
        self.assertEqual(remove, [])

    def test_an_unchanged_size_writes_nothing(self):
        # The whole reason this returns a diff rather than a target set. The
        # tick runs every five minutes and a write that changes nothing still
        # appends a timeline event.
        add, remove = self.e.label_updates(["SP::5", "bug"], 5)
        self.assertEqual(add, [])
        self.assertEqual(remove, [])

    def test_a_hand_typed_capitalisation_of_the_same_size_writes_nothing(self):
        # GitHub treats two label names differing only in case as one label.
        # Rewriting `sp::5` to `SP::5` would add the label GitHub resolves to
        # the existing one and then delete that same one by its real name — the
        # issue would lose its size and get it back every five minutes.
        for spelling in ("sp::5", "Sp::5", "SP :: 5"):
            with self.subTest(spelling=spelling):
                add, remove = self.e.label_updates([spelling], 5)
                self.assertEqual(add, [])
                self.assertEqual(remove, [])

    def test_a_new_size_replaces_the_old_one(self):
        add, remove = self.e.label_updates(["SP::3"], 8)
        self.assertEqual(add, ["SP::8"])
        self.assertEqual(remove, ["SP::3"])

    def test_mutual_exclusion_is_enforced_here_not_by_the_platform(self):
        # GitHub will hold `SP::2` and `SP::8` on the same issue quite happily,
        # and then "how big is this?" has two answers. Every write must
        # therefore carry its own removals.
        add, remove = self.e.label_updates(["SP::2", "SP::8", "SP::13"], 5)
        self.assertEqual(add, ["SP::5"])
        self.assertCountEqual(remove, ["SP::2", "SP::8", "SP::13"])

    def test_exclusion_spans_every_accepted_scope(self):
        # One value per SIZE, not one per spelling: an issue left carrying
        # `points::8` beside `SP::3` still has two answers.
        add, remove = self.e.label_updates(
            ["points::8", "weight::2", "size::13"], 3)
        self.assertEqual(add, ["SP::3"])
        self.assertCountEqual(remove, ["points::8", "weight::2", "size::13"])

    def test_a_size_label_naming_no_number_is_still_cleared(self):
        # Scope-owned, not value-owned: otherwise a typo survives every
        # estimate and permanently contradicts the real size.
        add, remove = self.e.label_updates(["SP::later", "size::XL"], 3)
        self.assertEqual(add, ["SP::3"])
        self.assertCountEqual(remove, ["SP::later", "size::XL"])

    def test_removals_carry_the_repositorys_own_spelling(self):
        # GitHub deletes a label from an issue by name in the URL, so a
        # normalised name would delete nothing and the stale size would stay.
        _, remove = self.e.label_updates(["Story Points::8"], 3)
        self.assertEqual(remove, ["Story Points::8"])

    def test_nothing_outside_the_size_scopes_is_ever_removed(self):
        # A model pin, an approval gate and a deferral all outlive an estimate.
        # Clearing one as a side effect of sizing would undo a human's
        # instruction silently.
        _, remove = self.e.label_updates(
            ["bug", "model::kimi/k3", "approval", "Next Sprint",
             "status::in-progress", "priority::high"], 5)
        self.assertEqual(remove, [])

    def test_an_off_scale_estimate_is_coerced_before_it_is_written(self):
        # The agent is told the scale but is not trusted to honour it.
        add, _ = self.e.label_updates([], 4)
        self.assertEqual(add, ["SP::5"])
        add, _ = self.e.label_updates([], "7")
        self.assertEqual(add, ["SP::8"])

    def test_something_that_is_not_a_size_is_a_programming_error(self):
        # normalise() already rounds 4 and 7 onto the scale, so reaching here
        # means the caller passed something that is not a positive number.
        for bad in ("big", "", None, 0, -3):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.e.label_updates([], bad)

    def test_a_written_size_reads_back_as_the_same_number(self):
        for points in self.e.story_points.usable_scale():
            with self.subTest(points=points):
                add, _ = self.e.label_updates([], points)
                self.assertEqual(self.e.points_of_labels(add), points)


class EstimateRequestLifecycle(unittest.TestCase):
    def setUp(self):
        self.e = load("story_estimate")

    def test_the_request_label_is_seen(self):
        self.assertTrue(self.e.wants_estimate(["estimate"]))
        self.assertTrue(self.e.wants_estimate([{"name": "Estimate"}]))
        self.assertFalse(self.e.wants_estimate(["bug", "SP::3"]))

    def test_the_request_is_a_request_and_not_a_result(self):
        # It means "size this, I have not" — it never carries the number.
        self.assertIsNone(self.e.points_of_labels(["estimate"]))

    def test_answering_the_request_removes_it(self):
        # The label set has to say what is still outstanding rather than
        # accumulating history, or the bot re-estimates the same issue forever.
        add, remove = self.e.label_updates(["estimate", "bug"], 3)
        self.assertEqual(add, ["SP::3"])
        self.assertEqual(remove, ["estimate"])

    def test_a_re_estimate_of_an_already_sized_issue_still_clears_the_request(self):
        # Same number, but the request must go — otherwise the ask repeats on
        # every tick and each one costs a model call.
        add, remove = self.e.label_updates(["SP::5", "estimate"], 5)
        self.assertEqual(add, [])
        self.assertEqual(remove, ["estimate"])

    def test_work_is_pending_when_asked_for_or_simply_missing(self):
        # Both halves are needed: the label is how a human asks for a re-size,
        # the missing size is how the ordinary unestimated issue arrives, and
        # nobody labels those.
        self.assertTrue(self.e.needs_estimate(["bug"]))
        self.assertTrue(self.e.needs_estimate(["SP::3", "estimate"]))
        self.assertFalse(self.e.needs_estimate(["SP::3"]))


class ModelPinIsReadOnly(unittest.TestCase):
    def setUp(self):
        self.e = load("story_estimate")

    def test_every_scope_people_type_is_read(self):
        for spelling in ("model::kimi/k3", "Model::kimi/k3", "ki::kimi/k3",
                         "KI::kimi/k3", "llm::kimi/k3", "ai::kimi/k3"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.e.model_label([spelling]), "kimi/k3")

    def test_a_bare_vendor_name_is_read(self):
        for vendor in self.e.MODEL_PROVIDERS:
            with self.subTest(vendor=vendor):
                self.assertEqual(self.e.model_label([vendor]), vendor)

    def test_a_full_id_is_read_without_a_scope(self):
        self.assertEqual(
            self.e.model_label(["minimax/MiniMax-M2.7"]), "minimax/MiniMax-M2.7")

    def test_an_unknown_word_is_not_a_routing_instruction(self):
        # `model::anything` is explicit, but a project that happens to label an
        # issue with a word we decided means something would have its work
        # silently re-routed.
        for label in ("bug", "docs/readme", "backend", "SP::5", "estimate"):
            with self.subTest(label=label):
                self.assertEqual(self.e.model_label([label]), "")

    def test_the_first_pin_wins(self):
        # Two model labels is a contradiction with no sensible resolution.
        # Picking one beats picking neither, because neither means a run on a
        # model nobody asked for.
        self.assertEqual(
            self.e.model_label(["model::kimi/k3", "minimax"]), "kimi/k3")

    def test_a_pin_is_never_written(self):
        # Nothing in this module produces one: it is a human's instruction to
        # the bot, not a fact the bot discovers.
        written = {d["name"] for d in self.e.label_definitions()}
        for name in written:
            with self.subTest(name=name):
                self.assertFalse(self.e.is_model_pin(name))

    def test_a_pin_is_never_cleared(self):
        _, remove = self.e.label_updates(
            ["model::kimi/k3", "ki::minimax", "SP::2"], 5)
        self.assertEqual(remove, ["SP::2"])


class NextSprintDefersImplementationOnly(unittest.TestCase):
    def setUp(self):
        self.e = load("story_estimate")

    def test_every_spelling_a_human_types_is_understood(self):
        # Comparison strips everything that is not a letter or a digit, because
        # these are the same instruction typed by five people.
        for spelling in ("next sprint", "Next Sprint", "next-sprint",
                         "next_sprint", "NextSprint", "nextsprint",
                         "plan::Next Sprint"):
            with self.subTest(spelling=spelling):
                self.assertTrue(self.e.deferred_to_next_sprint([spelling]))

    def test_a_similar_label_is_not_the_deferral(self):
        for spelling in ("sprint", "next", "next sprint planning", "sprint::4"):
            with self.subTest(spelling=spelling):
                self.assertFalse(self.e.deferred_to_next_sprint([spelling]))

    def test_a_deferred_story_is_still_estimated(self):
        # Sizing is what sprint planning needs in order to decide whether the
        # story fits in the NEXT sprint, so a deferred issue is exactly the one
        # whose size is most worth knowing.
        labels = ["Next Sprint", "estimate"]
        self.assertTrue(self.e.needs_estimate(labels))
        add, remove = self.e.label_updates(labels, 3)
        self.assertEqual(add, ["SP::3"])
        self.assertEqual(remove, ["estimate"])
        self.assertNotIn("Next Sprint", remove)

    def test_rollover_clears_it_using_the_repositorys_own_spelling(self):
        add, remove = self.e.sprint_rollover_updates(
            ["Next Sprint", "bug", "next-sprint"])
        self.assertEqual(add, [])
        self.assertEqual(remove, ["Next Sprint", "next-sprint"])

    def test_rollover_on_an_issue_that_was_never_deferred_writes_nothing(self):
        # Rollover touches every open issue in every permitted repository. A
        # no-op that still writes would stamp all of them.
        self.assertEqual(self.e.sprint_rollover_updates(["bug", "SP::3"]),
                         ([], []))


class ApprovalFailsOpen(unittest.TestCase):
    def setUp(self):
        self.e = load("story_estimate")

    def test_the_spellings_that_gate_a_merge(self):
        for spelling in ("approval", "Approval", "approve", "Approval Required",
                         "approval-required", "needs approval",
                         "requires approval", "Freigabe", "gate::approval"):
            with self.subTest(spelling=spelling):
                self.assertTrue(self.e.requires_approval([spelling]))

    def test_a_word_that_only_looks_like_it_is_not_a_gate(self):
        # A false positive parks a finished PR on a human who was never asked
        # for anything. `approved` is the opposite statement.
        for spelling in ("approved", "reviewed", "approval-notes", "bug"):
            with self.subTest(spelling=spelling):
                self.assertFalse(self.e.requires_approval([spelling]))

    def test_an_unreadable_label_list_means_no_gate(self):
        # THE point of this test. If a bad response made this answer True,
        # every PR in every repository would stop merging for a reason nobody
        # could see from the issue. Failing open loses the gate on one story;
        # branch protection and required reviews are still underneath.
        for broken in (None, Exploding(), 42, object(), {"labels": None}):
            with self.subTest(shape=type(broken).__name__):
                self.assertFalse(self.e.requires_approval(broken))

    def test_the_gate_is_never_set_by_the_bot(self):
        add, _ = self.e.label_updates(["bug"], 5)
        self.assertFalse(any(self.e.is_approval_request(n) for n in add))

    def test_the_gate_is_never_cleared_by_the_bot(self):
        # Its whole purpose is to survive every run of the story, so that a
        # merge attempt weeks later still stops at the same gate.
        _, remove = self.e.label_updates(
            ["approval", "Freigabe", "SP::2", "estimate"], 5)
        self.assertCountEqual(remove, ["SP::2", "estimate"])


class LabelDefinitions(unittest.TestCase):
    def setUp(self):
        self.e = load("story_estimate")

    def test_every_size_the_bot_writes_can_be_created(self):
        # GitHub will not put a label on an issue until the label exists in the
        # repository, so a size we cannot create fails the first estimate in
        # every fresh repo with a 422 nobody reads.
        defined = {d["name"] for d in self.e.label_definitions()}
        for points in self.e.story_points.usable_scale():
            with self.subTest(points=points):
                self.assertIn(self.e.label_for(points), defined)

    def test_the_labels_humans_apply_can_be_created_too(self):
        defined = {d["name"] for d in self.e.label_definitions()}
        self.assertTrue(any(self.e.is_estimate_request(n) for n in defined))
        self.assertTrue(any(self.e.is_next_sprint(n) for n in defined))
        self.assertTrue(any(self.e.is_approval_request(n) for n in defined))

    def test_definitions_are_well_formed(self):
        for d in self.e.label_definitions():
            with self.subTest(label=d["name"]):
                self.assertTrue(d["name"].strip())
                self.assertRegex(d["color"], r"^[0-9a-f]{6}$")
                self.assertTrue(d["description"].strip())

    def test_each_size_definition_reads_back_as_its_own_number(self):
        for d in self.e.label_definitions():
            if not self.e.is_size_label(d["name"]):
                continue
            with self.subTest(label=d["name"]):
                self.assertIsNotNone(self.e.points_of_label(d["name"]))


if __name__ == "__main__":
    unittest.main()

"""The five-value status model, on a platform that ships two states.

Each test here names the decision it protects. The expensive mistakes in this
area are all silent: a status that reads back differently from what was
written strands an issue nobody will look at again, and a label write that
fires on every unchanged tick buries the timeline.
"""

import unittest

from harness import load


class StatusReading(unittest.TestCase):
    def setUp(self):
        self.s = load("issue_status")

    def test_an_untouched_open_issue_is_to_do(self):
        # The default has to be the pickable one. If absence of a label read
        # as "unknown" the planner would have to write a label to every issue
        # in every permitted repo before it could plan anything.
        self.assertEqual(self.s.status_of([]), self.s.TO_DO)
        self.assertTrue(self.s.is_workable(self.s.status_of([])))

    def test_in_progress_label_is_read_back(self):
        self.assertEqual(
            self.s.status_of(["status::in-progress"]), self.s.IN_PROGRESS)

    def test_unrelated_labels_do_not_change_the_status(self):
        self.assertEqual(
            self.s.status_of(["bug", "On Hold", "SP::3"]), self.s.TO_DO)

    def test_closed_as_completed_is_done(self):
        self.assertEqual(
            self.s.status_of([], state="closed", state_reason="completed"),
            self.s.DONE)

    def test_closed_as_not_planned_is_wont_do(self):
        # The distinction this whole module exists for: a revoked issue must
        # not be recorded as delivered against a sprint.
        self.assertEqual(
            self.s.status_of([], state="closed", state_reason="not_planned"),
            self.s.WONT_DO)

    def test_not_planned_plus_duplicate_label_is_duplicate(self):
        # not_planned covers both terminal-but-undelivered cases; only the
        # label separates them.
        self.assertEqual(
            self.s.status_of(["status::duplicate"], state="closed",
                             state_reason="not_planned"),
            self.s.DUPLICATE)

    def test_a_closed_issue_with_no_reason_is_done(self):
        # Issues closed before state_reason existed, and any API response that
        # omits it. Treating those as "won't do" would erase delivery history.
        self.assertEqual(
            self.s.status_of([], state="closed", state_reason=None),
            self.s.DONE)

    def test_close_reason_beats_a_stale_in_progress_label(self):
        # The ordinary way this happens: a human closes the issue while a run
        # is still in flight, so nothing ever clears the label.
        self.assertEqual(
            self.s.status_of(["status::in-progress"], state="closed",
                             state_reason="completed"),
            self.s.DONE)

    def test_contradictory_labels_resolve_instead_of_raising(self):
        # A planner that raises on one mislabelled issue stops planning every
        # other issue in the same tick. Most-advanced wins.
        self.assertEqual(
            self.s.status_of(["status::in-progress", "status::wont-do"]),
            self.s.WONT_DO)

    def test_hand_typed_spellings_are_understood(self):
        for spelling in ("Status::In Progress", "status::WIP",
                         "status::in progress", "status::doing"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    self.s.status_of([spelling]), self.s.IN_PROGRESS)

    def test_apostrophes_do_not_change_meaning(self):
        for spelling in ("status::wont-do", "status::won't do",
                         "status::wontfix"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.s.normalize(spelling), self.s.WONT_DO)

    def test_only_workable_statuses_are_workable(self):
        self.assertTrue(self.s.is_workable(self.s.TO_DO))
        self.assertTrue(self.s.is_workable(self.s.IN_PROGRESS))
        for finished in (self.s.DONE, self.s.WONT_DO, self.s.DUPLICATE):
            with self.subTest(status=finished):
                self.assertFalse(self.s.is_workable(finished))


class StatusWriting(unittest.TestCase):
    def setUp(self):
        self.s = load("issue_status")

    def test_setting_in_progress_on_a_fresh_issue_adds_one_label(self):
        add, remove = self.s.label_updates([], self.s.IN_PROGRESS)
        self.assertEqual(add, ["status::in-progress"])
        self.assertEqual(remove, [])

    def test_an_unchanged_status_writes_nothing(self):
        # This is the whole reason label_updates returns a diff rather than a
        # target set. The tick runs every five minutes; a write that changes
        # nothing still appends a timeline event.
        add, remove = self.s.label_updates(
            ["status::in-progress", "bug"], self.s.IN_PROGRESS)
        self.assertEqual(add, [])
        self.assertEqual(remove, [])

    def test_moving_to_to_do_clears_the_status_label(self):
        add, remove = self.s.label_updates(
            ["status::in-progress"], self.s.TO_DO)
        self.assertEqual(add, [])
        self.assertEqual(remove, ["status::in-progress"])

    def test_mutual_exclusion_is_enforced_here_not_by_the_platform(self):
        # GitHub will happily hold both labels at once. Every transition must
        # therefore carry its own removals.
        add, remove = self.s.label_updates(
            ["status::in-progress", "status::duplicate"], self.s.WONT_DO)
        self.assertEqual(add, ["status::wont-do"])
        self.assertCountEqual(
            remove, ["status::in-progress", "status::duplicate"])

    def test_an_unrecognised_status_label_is_still_cleared(self):
        # Prefix-owned, not value-owned: otherwise a typo survives every
        # transition and permanently contradicts the real status.
        add, remove = self.s.label_updates(
            ["status::whatever"], self.s.IN_PROGRESS)
        self.assertEqual(add, ["status::in-progress"])
        self.assertEqual(remove, ["status::whatever"])

    def test_non_status_labels_are_never_removed(self):
        add, remove = self.s.label_updates(
            ["bug", "On Hold", "SP::5"], self.s.IN_PROGRESS)
        self.assertEqual(remove, [])

    def test_done_carries_no_label(self):
        # Done is fully described by state_reason=completed. A label would be
        # a second source of truth that can disagree with the first.
        self.assertIsNone(self.s.label_for(self.s.DONE))
        add, remove = self.s.label_updates(
            ["status::in-progress"], self.s.DONE)
        self.assertEqual(add, [])
        self.assertEqual(remove, ["status::in-progress"])

    def test_an_unknown_status_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            self.s.label_updates([], "shipped-ish")


class CloseSemantics(unittest.TestCase):
    def setUp(self):
        self.s = load("issue_status")

    def test_delivered_and_revoked_close_differently(self):
        self.assertEqual(self.s.close_reason(self.s.DONE), "completed")
        self.assertEqual(self.s.close_reason(self.s.WONT_DO), "not_planned")
        self.assertEqual(self.s.close_reason(self.s.DUPLICATE), "not_planned")

    def test_open_statuses_have_no_close_reason(self):
        self.assertIsNone(self.s.close_reason(self.s.TO_DO))
        self.assertIsNone(self.s.close_reason(self.s.IN_PROGRESS))

    def test_a_round_trip_preserves_every_status(self):
        # The property that matters: whatever the bot writes, the next tick
        # must read back as the same status. A break here strands issues.
        for status in self.s.STATUSES:
            with self.subTest(status=status):
                add, _ = self.s.label_updates([], status)
                reason = self.s.close_reason(status)
                state = "closed" if status in self.s.TERMINAL else "open"
                self.assertEqual(
                    self.s.status_of(add, state=state, state_reason=reason),
                    status)


class StatusFromNeutralIntent(unittest.TestCase):
    """`status_of_item` — the entry point the planners actually use.

    They never see a host's native close reason. They see whether the work was
    DELIVERED or REVOKED, because one of the two hosts has no such field and
    writes the intent as a label instead. This is where that vocabulary meets
    the status model; `status_of` stays as the one that speaks the native
    field, for a reader that already holds one.
    """

    def setUp(self):
        self.s = load("issue_status")

    def test_delivered_and_revoked_reach_the_statuses_they_mean(self):
        self.assertEqual(
            self.s.status_of_item([], state="closed", closed_as="delivered"),
            self.s.DONE)
        self.assertEqual(
            self.s.status_of_item([], state="closed", closed_as="revoked"),
            self.s.WONT_DO)

    def test_a_close_with_no_recorded_intent_is_a_delivery(self):
        # Every item closed before any host recorded intent has none, and all
        # of them shipped. Reading absence as "called off" would revoke the
        # entire history at once, and the bot reopens what it thinks was
        # abandoned.
        self.assertEqual(self.s.status_of_item([], state="closed"),
                         self.s.DONE)

    def test_an_open_item_still_reads_its_status_from_the_labels(self):
        # The close intent is irrelevant while the item is open, and the
        # label is the only thing that says whether work has started.
        self.assertEqual(
            self.s.status_of_item(["status::in progress"], state="open"),
            self.s.IN_PROGRESS)
        self.assertEqual(self.s.status_of_item([], state="open"),
                         self.s.TO_DO)

    def test_it_agrees_with_the_native_reading_on_every_status(self):
        # Two entry points to one model. If they can disagree, then which
        # planner asked decides what the status is.
        for status in self.s.STATUSES:
            with self.subTest(status=status):
                add, _ = self.s.label_updates([], status)
                reason = self.s.close_reason(status)
                state = "closed" if status in self.s.TERMINAL else "open"
                closed_as = None
                if reason == "completed":
                    closed_as = "delivered"
                elif reason == "not_planned":
                    closed_as = "revoked"
                self.assertEqual(
                    self.s.status_of_item(add, state=state,
                                          closed_as=closed_as),
                    self.s.status_of(add, state=state, state_reason=reason))


class LabelDefinitions(unittest.TestCase):
    def setUp(self):
        self.s = load("issue_status")

    def test_every_written_label_can_be_created(self):
        # A transition that writes a label the bot cannot create fails on any
        # repo that has not been set up by hand.
        defined = {d["name"] for d in self.s.label_definitions()}
        for status in self.s.STATUSES:
            wanted = self.s.label_for(status)
            if wanted is not None:
                with self.subTest(status=status):
                    self.assertIn(wanted, defined)

    def test_definitions_are_well_formed(self):
        for d in self.s.label_definitions():
            with self.subTest(label=d["name"]):
                self.assertTrue(self.s.is_status_label(d["name"]))
                self.assertRegex(d["color"], r"^[0-9a-f]{6}$")
                self.assertTrue(d["description"].strip())


if __name__ == "__main__":
    unittest.main()

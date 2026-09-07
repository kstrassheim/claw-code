"""The `On Hold` label must come off when the person answers.

The ask the bot posts offers two ways out — "Reply mentioning `@bot` (or
remove the **On Hold** label) and I'll proceed". Only the parenthetical was
ever wired: `remove_label` existed on every forge with no caller anywhere, and
the label gate dropped the issue before a single comment was read. So the
reply the message asks for did nothing, on every host. Observed on eight
issues answered inside ten minutes of each other, not one of which moved.

The bar here is deliberately HIGHER than `human_has_answered`, which ranks a
marker park and only asks whether a person spoke last. This park guards
destructive-sounding work, so what lifts it is the act the ask names: a reply
that @-mentions the bot, after the bot asked. Everything unreadable stays
parked — a park that outlives its answer costs a reply, a park lifted on an
unanswered question costs whatever the issue asked for.
"""

import unittest

import fakeforge
import forge
from fakeforge import note
from harness import load

ASK = "🛑 DESTRUCTIVE CHANGE — PLEASE CONFIRM\n\n@someone — I need clarification"


class HoldReleasesWhenAnswered(unittest.TestCase):
    def setUp(self):
        self.h = load("heartbeat-issue-tick")
        self.forge = fakeforge.FakeForge(identity="bot")
        self.h.FORGES = forge.Forges([self.forge])

    def release(self, notes, labels=("On Hold",)):
        self.forge.notes[5] = list(notes)
        issue = {"number": 5, "labels": list(labels)}
        return self.h.release_hold(self.forge, "o/r", issue, "bot")

    # -- what lifts it ----------------------------------------------------

    def test_a_reply_mentioning_the_bot_after_the_ask_releases(self):
        self.assertTrue(self.release([
            note(ASK, "bot", 1),
            note("@bot go ahead, there is no staging there", "human", 2),
        ]))
        self.assertEqual(self.forge.writes_of("unlabel"), ["On Hold"])

    def test_the_label_is_removed_by_the_name_the_issue_uses(self):
        # `_fold` recognises the park through scope prefixes and punctuation,
        # but the host deletes by exact name — so passing the literal
        # "On Hold" would 404 on an issue that spells it differently.
        self.assertTrue(self.release([
            note(ASK, "bot", 1),
            note("@bot yes please", "human", 2),
        ], labels=("Status::On Hold",)))
        self.assertEqual(self.forge.writes_of("unlabel"), ["Status::On Hold"])

    def test_a_park_with_no_ask_note_anchors_on_the_bots_last_word(self):
        # The solver parks too (fixer-runner's `park`), and that park carries
        # no ASK marker. The bot's newest note is where that wait started.
        self.assertTrue(self.release([
            note("I need a decision before I continue.", "bot", 1),
            note("@bot close it", "human", 2),
        ]))

    # -- what does not ----------------------------------------------------

    def test_a_reply_without_a_mention_is_not_an_answer(self):
        # Somebody talking about the issue is not somebody answering it.
        self.assertFalse(self.release([
            note(ASK, "bot", 1),
            note("this one bit me too", "human", 2),
        ]))
        self.assertEqual(self.forge.writes_of("unlabel"), [])

    def test_a_mention_that_predates_the_ask_is_not_an_answer(self):
        # It answered whatever came before, not the question just asked.
        self.assertFalse(self.release([
            note("@bot have a look at this", "human", 1),
            note(ASK, "bot", 2),
        ]))

    def test_the_bot_mentioning_itself_is_not_an_answer(self):
        self.assertFalse(self.release([
            note(ASK, "bot", 1),
            note("@bot still waiting on a person", "bot", 2),
        ]))

    def test_a_park_nobody_asked_for_is_not_the_bots_to_lift(self):
        # No note from the bot at all: a human applied the label by hand, and
        # a human takes it off by hand.
        self.assertFalse(self.release([
            note("@bot do this", "human", 1),
        ]))

    def test_an_issue_without_the_label_is_not_released(self):
        self.assertFalse(self.release([
            note(ASK, "bot", 1),
            note("@bot go", "human", 2),
        ], labels=("bug",)))

    # -- failure direction ------------------------------------------------

    def test_an_unreadable_thread_stays_parked(self):
        # Opposite direction to human_has_answered, and deliberately so.
        self.forge.raises["comments"] = forge.ForgeError("boom")
        self.assertFalse(self.release([note(ASK, "bot", 1)]))

    def test_a_failed_removal_stays_parked(self):
        # The label is still on the issue, so the planner must still treat it
        # as parked — otherwise it spawns every tick against a live park.
        self.forge.writes_fail = True
        self.assertFalse(self.release([
            note(ASK, "bot", 1),
            note("@bot go ahead", "human", 2),
        ]))


class TheParkLabelIsFoundByItsSpelling(unittest.TestCase):
    """`on_hold_label_name` — which label to delete, given the issue.

    Separate from the release decision because the two fail differently: not
    recognising the park leaves the issue stuck, and recognising it under a
    name the host does not have 404s the delete.
    """

    def setUp(self):
        self.h = load("heartbeat-issue-tick")

    def name_of(self, *labels):
        return self.h.on_hold_label_name({"number": 5, "labels": list(labels)})

    def test_the_plain_spelling(self):
        self.assertEqual(self.name_of("bug", "On Hold"), "On Hold")

    def test_a_scope_prefix_is_tolerated_and_returned_intact(self):
        self.assertEqual(self.name_of("Status::On Hold"), "Status::On Hold")

    def test_punctuation_and_case_are_tolerated(self):
        self.assertEqual(self.name_of("on-hold"), "on-hold")
        self.assertEqual(self.name_of("ON_HOLD"), "ON_HOLD")

    def test_an_issue_with_no_park_has_no_name(self):
        self.assertEqual(self.name_of("bug", "Priority::High"), "")

    def test_no_labels_at_all(self):
        self.assertEqual(self.h.on_hold_label_name({"number": 5}), "")


class TheAskNoteIsFound(unittest.TestCase):
    """`lexical_guard.ask_note_id` — the anchor both halves of the fix use.

    The planner reads it to know when the wait started; the solver anchors its
    first-run cursor on it so the reply that arrived before the run existed is
    still unread. It shipped with no caller at all until now, so this is its
    first exercise.
    """

    def setUp(self):
        self.g = load("lexical_guard")

    def test_the_bots_ask_is_found(self):
        self.assertEqual(self.g.ask_note_id([
            note("chatter", "human", 1),
            note(ASK, "bot", 2),
        ], "bot"), 2)

    def test_the_newest_ask_wins_when_it_was_asked_twice(self):
        # A reworded issue can be asked about again; the live wait is the
        # latest one, and anchoring on the older would read the first reply
        # as an answer to the second question.
        self.assertEqual(self.g.ask_note_id([
            note(ASK, "bot", 2),
            note("@bot ok", "human", 3),
            note(ASK, "bot", 7),
        ], "bot"), 7)

    def test_an_ask_worded_by_somebody_else_is_not_the_bots(self):
        self.assertIsNone(self.g.ask_note_id([note(ASK, "human", 1)], "bot"))

    def test_an_ordinary_bot_comment_is_not_an_ask(self):
        self.assertIsNone(self.g.ask_note_id([
            note("opened a pull request", "bot", 1),
        ], "bot"))

    def test_no_notes_at_all(self):
        self.assertIsNone(self.g.ask_note_id([], "bot"))


class WhatCountsAsAnAnswer(unittest.TestCase):
    """`answer_after` — one definition, because two gates read it.

    release_hold takes the label off when this returns an id, and
    ask_before_spawning decides on the same reply. If they could disagree, an
    issue would be released into a gate that still refuses it — which is the
    exact shape of the bug both were fixed for.
    """

    def setUp(self):
        self.h = load("heartbeat-issue-tick")

    def test_a_mention_after_the_anchor_is_the_answer(self):
        self.assertEqual(self.h.answer_after([
            note("@bot yes", "human", 9)], "bot", 5), 9)

    def test_the_first_answer_wins_not_the_last(self):
        # The wait ended at the first reply; later chatter did not end it again.
        self.assertEqual(self.h.answer_after([
            note("@bot yes", "human", 7),
            note("@bot and one more thing", "human", 9)], "bot", 5), 7)

    def test_a_note_at_the_anchor_itself_is_not_after_it(self):
        self.assertIsNone(self.h.answer_after([
            note("@bot yes", "human", 5)], "bot", 5))

    def test_the_bot_cannot_answer_itself(self):
        self.assertIsNone(self.h.answer_after([
            note("@bot still waiting", "bot", 9)], "bot", 5))

    def test_a_reply_without_a_mention_is_not_an_answer(self):
        self.assertIsNone(self.h.answer_after([
            note("looks right to me", "human", 9)], "bot", 5))

    def test_the_mention_is_matched_case_insensitively(self):
        self.assertEqual(self.h.answer_after([
            note("@BOT go", "human", 9)], "bot", 5), 9)

    def test_no_anchor_means_no_answer(self):
        # Nobody asked, so nothing is being answered.
        self.assertIsNone(self.h.answer_after([
            note("@bot go", "human", 9)], "bot", None))

    def test_a_note_with_an_unusable_id_is_skipped_not_fatal(self):
        self.assertEqual(self.h.answer_after([
            {"id": None, "body": "@bot go", "author": {"username": "human"}},
            note("@bot go", "human", 9)], "bot", 5), 9)


class AnAnsweredQuestionStopsBlockingTheSpawn(unittest.TestCase):
    """The destructive-wording gate must also accept the answer.

    `ask_before_spawning` returned True forever once the question was on the
    record: `already_asked` only asks whether the bot ASKED, never whether
    anybody answered. Its comment claimed the release was "a human taking the
    On Hold label off, which is checked before this" — but an issue reaching
    this gate has already passed the label check, so the label was off and it
    still refused. A guard-questioned issue could not be spawned by ANY means:
    not by answering, not by removing the label by hand. Seen on an issue
    answered "its ok continue" that then sat for four days.
    """

    DESTRUCTIVE = "Remove the legacy auth tests that keep failing in CI"

    def setUp(self):
        self.h = load("heartbeat-issue-tick")
        self.forge = fakeforge.FakeForge(identity="bot")
        self.h.FORGES = forge.Forges([self.forge])

    def blocked(self, notes):
        """True ⟺ the gate refuses to spawn."""
        self.forge.notes[5] = list(notes)
        return self.h.ask_before_spawning(
            self.forge, "o/r",
            {"number": 5, "title": self.DESTRUCTIVE, "body": ""}, "bot")

    def test_an_unanswered_question_still_blocks(self):
        self.assertTrue(self.blocked([note(ASK, "bot", 1)]))

    def test_an_answer_mentioning_the_bot_unblocks_it(self):
        self.assertFalse(self.blocked([
            note(ASK, "bot", 1),
            note("@bot its ok continue", "human", 2),
        ]))

    def test_chatter_without_a_mention_does_not_unblock_it(self):
        self.assertTrue(self.blocked([
            note(ASK, "bot", 1),
            note("this has bitten me too", "human", 2),
        ]))

    def test_an_answer_before_the_question_does_not_count(self):
        self.assertTrue(self.blocked([
            note("@bot go ahead", "human", 1),
            note(ASK, "bot", 2),
        ]))

    def test_a_reworded_issue_is_asked_about_once_and_then_waits(self):
        # No ask on record yet: the gate asks, parks, and blocks.
        self.assertTrue(self.blocked([]))
        self.assertEqual(self.forge.writes_of("labels"), [["On Hold"]])

    def test_human_to_ask_answers_one_name_or_none(self):
        # The resolver by name, so the ledger sees it and a rename has to come
        # past a test: it is the only thing standing between this question and
        # a department.
        self.forge.owner = "creator"
        issue = {"number": 5, "title": self.DESTRUCTIVE, "body": ""}
        self.assertEqual(
            self.h.human_to_ask(self.forge, "group/team/app",
                                dict(issue, author="ada"), "bot"), "ada")
        self.assertEqual(
            self.h.human_to_ask(self.forge, "group/team/app",
                                dict(issue, author="bot"), "bot"), "creator")
        self.forge.owner = ""
        self.assertEqual(
            self.h.human_to_ask(self.forge, "group/team/app",
                                dict(issue, author="bot"), "bot"), "")

    def test_the_question_goes_to_the_person_who_filed_it(self):
        """WHO the question is put to — and it is never the group.

        The mention was the first segment of the project path. On the hosted
        GitLab that is a department: `@group` asked every member of
        one to confirm a destructive change, on two issues, minutes apart.
        """
        self.forge.notes[5] = []
        self.forge.owner = "creator"
        self.h.ask_before_spawning(
            self.forge, "group/team/app",
            {"number": 5, "title": self.DESTRUCTIVE, "body": "",
             "author": "ada"}, "bot")
        asked = self.forge.writes_of("comment")[0]
        self.assertIn("@ada", asked)
        self.assertNotIn("@group", asked)

    def test_an_issue_the_bot_filed_asks_the_repository_creator(self):
        # The tester files its own findings. Asking the bot to confirm the
        # bot's request answers nothing — and the fallback is still ONE
        # account, resolved by the host, never the path.
        self.forge.notes[5] = []
        self.forge.owner = "creator"
        self.h.ask_before_spawning(
            self.forge, "group/team/app",
            {"number": 5, "title": self.DESTRUCTIVE, "body": "",
             "author": "bot"}, "bot")
        asked = self.forge.writes_of("comment")[0]
        self.assertIn("@creator", asked)
        self.assertNotIn("@group", asked)

    def test_nobody_to_name_still_asks_the_question(self):
        # A guard that skips itself because it could not find a name is a
        # guard that lets the destructive change through.
        self.forge.notes[5] = []
        self.forge.owner = ""
        self.h.ask_before_spawning(
            self.forge, "group/team/app",
            {"number": 5, "title": self.DESTRUCTIVE, "body": "",
             "author": "bot"}, "bot")
        asked = self.forge.writes_of("comment")[0]
        self.assertIn("DESTRUCTIVE CHANGE", asked)
        self.assertNotIn("@ ", asked)
        self.assertNotIn("@group", asked)

    def test_wording_that_is_not_destructive_never_reaches_the_gate(self):
        self.forge.notes[5] = []
        self.assertFalse(self.h.ask_before_spawning(
            self.forge, "o/r",
            {"number": 5, "title": "Add a lint script", "body": ""}, "bot"))


if __name__ == "__main__":
    unittest.main()


class BothAsksCarryTheSameMarker(unittest.TestCase):
    """The planner and the solver ask the same question in two places.

    `ask_note_id` locates the open question by searching for
    `lexical_guard.ASK_MARKER`, and both `release_hold` and
    `ask_before_spawning` anchor on what it returns. An ask without the marker
    is invisible to them — the anchor falls back to an OLDER ask that does
    carry it, and any reply posted after THAT one is read as the answer to
    THIS one.

    ultimate-web-stack#90: the solver asked at 05:55:49, parked at 05:55:51,
    and the planner un-parked at 06:00:39 on the strength of a reply from the
    previous day. The question was never answered; the label just flapped.
    """

    def runner_source(self):
        import pathlib as _pl
        return (_pl.Path(__file__).resolve().parents[1]
                / "fixer-runner.sh").read_text()

    def test_the_solvers_ask_contains_the_marker(self):
        import lexical_guard
        body = self.runner_source().split("ASK_BODY=\"", 1)[1]
        body = body.split("\"\n", 1)[0]
        self.assertIn(lexical_guard.ASK_MARKER, body,
                      "the solver's ask is invisible to ask_note_id")

    def test_the_planners_ask_contains_the_marker(self):
        import lexical_guard
        note = lexical_guard.ask_note(
            {"hit": "delete the tests", "rule": "x"}, "someone", "bot")
        self.assertIn(lexical_guard.ASK_MARKER, note)

    def test_ask_note_id_finds_an_ask_in_either_wording(self):
        import lexical_guard
        for body in (lexical_guard.ask_note({"hit": "h"}, "someone", "bot"),
                     f"🛑 {lexical_guard.ASK_MARKER}\n\n@someone — I need "
                     "clarification before writing any code."):
            self.assertEqual(
                lexical_guard.ask_note_id([note(body, "bot", 42)], "bot"), 42,
                f"not recognised as an ask: {body[:60]!r}")


ESCALATION = ("⚠️ PR #203 is still failing after 4 fix attempts on `de36477`. "
              "I could not get the checks green on my own.\n\n"
              "@someone — could you take a look? Reply here and @-mention "
              "`@bot` and I will try again.")


class TheAnchorIsTheNewestAsk(unittest.TestCase):
    """A reply answers the question that was open when it was written.

    The bot asks more than once over an issue's life, and only the guard's ask
    carries a marker — the CI-red escalation and the merge-approval request do
    not. Anchoring on the marker alone therefore pins the wait to the FIRST
    question forever, and the answer to that one goes on releasing every park
    that comes after it.

    Observed on an issue: the guard asked on the 29th, the filer answered on
    the 31st, the solver escalated a red pipeline that evening. From then on the
    planner released the park every five minutes on the strength of the 31st's
    reply and the solver re-parked it ten seconds later, all night — three
    hundred label events on a question nobody had answered.
    """

    def setUp(self):
        self.h = load("heartbeat-issue-tick")
        self.forge = fakeforge.FakeForge(identity="bot")
        self.h.FORGES = forge.Forges([self.forge])

    def release(self, notes, labels=("On Hold",)):
        self.forge.notes[5] = list(notes)
        issue = {"number": 5, "labels": list(labels)}
        return self.h.release_hold(self.forge, "o/r", issue, "bot")

    def test_an_escalation_after_the_answer_is_a_wait_of_its_own(self):
        self.assertFalse(self.release([
            note(ASK, "bot", 1),
            note("@bot please continue, there is nothing wrong", "human", 2),
            note("Starting work on issue #66.", "bot", 3),
            note(ESCALATION, "bot", 4),
        ]))
        self.assertEqual(self.forge.writes_of("unlabel"), [])

    def test_answering_the_escalation_releases_it(self):
        self.assertTrue(self.release([
            note(ASK, "bot", 1),
            note("@bot please continue, there is nothing wrong", "human", 2),
            note(ESCALATION, "bot", 3),
            note("@bot the runner was out of disk, try again", "human", 4),
        ]))
        self.assertEqual(self.forge.writes_of("unlabel"), ["On Hold"])

    def test_an_ordinary_bot_note_does_not_swallow_the_answer(self):
        # The other direction, and the one that costs a deadlock: a progress
        # note names nobody, asks nothing, and must not move the anchor past a
        # reply that has already been given.
        self.assertTrue(self.release([
            note(ASK, "bot", 1),
            note("@bot go ahead", "human", 2),
            note("Starting work on issue #66 — switching to `issue-66-fix`.",
                 "bot", 3),
        ]))

    def test_the_bot_naming_only_itself_is_not_a_new_question(self):
        # Every ask closes by saying how to answer it. Reading that as a fresh
        # question would make each ask its own successor, and no reply could
        # ever be newer than the wait it answers.
        self.assertTrue(self.release([
            note(ASK, "bot", 1),
            note("@bot go ahead", "human", 2),
            note("Working on it — reply and @-mention `@bot` to interrupt.",
                 "bot", 3),
        ]))


class WhatCountsAsAskingAPerson(unittest.TestCase):
    """`asks_a_person` — the marker every park note shares.

    Only the lexical guard stamps `ASK_MARKER`. What the escalation, the
    merge-approval request and the guard's ask all do is NAME the person being
    waited on, so that is what the anchor reads.
    """

    def setUp(self):
        self.h = load("heartbeat-issue-tick")

    def asks(self, body):
        return self.h.asks_a_person(body, "bot")

    def test_naming_a_person_is_an_ask(self):
        self.assertTrue(self.asks("@someone — could you take a look?"))

    def test_naming_only_the_bot_is_not(self):
        self.assertTrue(not self.asks("reply and @-mention `@bot` to interrupt"))

    def test_the_word_mention_is_not_a_name(self):
        # `@-mention` is prose. Matching it would make every ask an ask of
        # somebody called "-mention".
        self.assertTrue(not self.asks("please @-mention me when it is green"))

    def test_a_note_that_names_nobody_is_not(self):
        self.assertTrue(not self.asks("Opened PR #203 for this issue."))

    def test_trailing_punctuation_does_not_hide_the_bots_own_name(self):
        self.assertTrue(not self.asks("ping @bot."))


class WhereTheOpenWaitStarted(unittest.TestCase):
    """`park_anchor` — the note a reply has to be newer than.

    Exercised directly as well as through `release_hold`, because the three
    answers it can give fail in three different ways: the wrong ask reads a
    stale reply as an answer, no ask at all leaves a solver-side park unliftable,
    and None on a park the bot did put there strands the issue.
    """

    def setUp(self):
        self.h = load("heartbeat-issue-tick")

    def anchor(self, notes):
        rows = [(n["id"], n["author"]["username"].lower(), n["body"])
                for n in notes]
        return self.h.park_anchor(rows, notes, "bot")

    def test_the_newest_ask_wins_over_the_guards_marker(self):
        # The escalation carries no ASK_MARKER; it is still the open question.
        self.assertEqual(self.anchor([
            note(ASK, "bot", 1),
            note("@bot go ahead", "human", 2),
            note(ESCALATION, "bot", 3),
        ]), 3)

    def test_the_guards_marker_counts_when_it_is_the_newest(self):
        self.assertEqual(self.anchor([
            note(ESCALATION, "bot", 1),
            note("@bot fixed the runner", "human", 2),
            note(ASK, "bot", 3),
        ]), 3)

    def test_a_park_with_no_ask_anchors_on_the_bots_last_word(self):
        # The solver's `park` posts prose that names nobody.
        self.assertEqual(self.anchor([
            note("I need a decision before I continue.", "bot", 4),
        ]), 4)

    def test_a_progress_note_does_not_become_the_anchor(self):
        self.assertEqual(self.anchor([
            note(ASK, "bot", 1),
            note("Opened PR #203 for this issue.", "bot", 2),
        ]), 1)

    def test_a_park_the_bot_never_asked_for_has_no_anchor(self):
        self.assertIsNone(self.anchor([note("@bot do this", "human", 1)]))

"""A park on a human must lift when the human answers.

An issue awaiting a person ranks LAST so the bot works something it can
influence. The original design let the SOLVER clear the park when it next ran
and saw the reply. That cannot work, and the failure is circular:

    ranked last  ->  never spawned  ->  marker never cleared  ->  ranked last

With any backlog at all the park becomes permanent, and answering the question
does not release it. Observed on an issue that sat seven hours after the human
had replied, the reply unread on the issue the whole time.

So the PLANNER decides it from the code host, without the solver running.
Both places a person can answer are checked, because a handoff asks them to
act in either: the issue, or the change request.

The planner asks a FORGE, not an API, so this drives a fake one. Which host
answered is not a question the park has an opinion about.
"""

import unittest

import fakeforge
import forge
from harness import load


class ParkLiftsWhenAnswered(unittest.TestCase):
    def setUp(self):
        self.h = load("heartbeat-issue-tick")
        self.forge = fakeforge.FakeForge(identity="bot")
        self.h.FORGES = forge.Forges([self.forge])

    def answered(self, issue_comments=None, linked=None,
                 cr_comments=None, verdicts=None):
        """One park decision, with only what a test cares about filled in."""
        f = self.forge
        f.notes[5] = list(issue_comments or [])
        f.linked[5] = list(linked or [])
        f.change_request_notes[7] = list(cr_comments or [])
        f.verdicts[7] = list(verdicts or [])
        return self.h.human_has_answered("o/r", {"number": 5}, "bot")

    def test_the_bots_unanswered_question_is_still_a_wait(self):
        self.assertFalse(self.answered(
            issue_comments=[fakeforge.note("@owner ?", "bot")]))

    def test_a_reply_on_the_issue_lifts_the_park(self):
        # The exact case that sat for seven hours.
        self.assertTrue(self.answered(issue_comments=[
            fakeforge.note("@owner ?", "bot"),
            fakeforge.note("fix it autonomously", "owner"),
        ]))

    def test_a_reply_on_the_change_request_lifts_it_too(self):
        # A handoff asks the person to act on the change request, so that is
        # where they often answer. Missing this parks an issue that was
        # answered.
        self.assertTrue(self.answered(
            issue_comments=[fakeforge.note("@owner ?", "bot")],
            linked=[7],
            cr_comments=[fakeforge.note("asked", "bot"),
                         fakeforge.note("go ahead", "owner")]))

    def test_a_review_by_a_person_lifts_it(self):
        self.assertTrue(self.answered(
            issue_comments=[fakeforge.note("@owner ?", "bot")],
            linked=[7],
            cr_comments=[fakeforge.note("asked", "bot")],
            verdicts=[{"author": "owner", "verdict": "commented"}]))

    def test_the_bots_own_review_does_not_count_as_an_answer(self):
        self.assertFalse(self.answered(
            issue_comments=[fakeforge.note("@owner ?", "bot")],
            linked=[7],
            cr_comments=[fakeforge.note("asked", "bot")],
            verdicts=[{"author": "bot", "verdict": "approved"}]))

    def test_no_comments_at_all_is_not_a_wait(self):
        # Nobody has been asked anything, so nobody is being waited on.
        self.assertTrue(self.answered(issue_comments=[]))

    def test_a_failure_to_read_resumes_rather_than_parks(self):
        # Being wrong this way costs one spawn that exits in seconds. Being
        # wrong the other way is a park that never lifts.
        self.forge.raises["comments"] = forge.ForgeError("boom")
        self.assertTrue(
            self.h.human_has_answered("o/r", {"number": 5}, "bot"))

    def test_an_unreadable_change_request_resumes_too(self):
        # Same direction, one question further in: the issue thread says the
        # bot spoke last, and the place the person was asked to answer cannot
        # be read at all.
        self.forge.notes[5] = [fakeforge.note("@owner ?", "bot")]
        self.forge.linked[5] = [7]
        self.forge.raises["change_request_comments"] = forge.ForgeError("boom")
        self.assertTrue(
            self.h.human_has_answered("o/r", {"number": 5}, "bot"))


if __name__ == "__main__":
    unittest.main()

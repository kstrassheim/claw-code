"""Every park the bot can enter has an act that ends it.

A park is a promise: the bot stops, a person does something, the bot resumes.
The failure mode is not a crash — it is silence. The label sits there, the
person does the thing the ask asked for, and nothing happens, because the act
they performed is not the act the release rule was looking for. Nobody gets an
error; the issue simply never moves again.

That is why the rule DEPENDS ON WHY THE ISSUE IS PARKED, and why the two
processes that decide it share one module:

    parked because the bot ASKED A QUESTION  -> a reply that @-mentions it
    parked because it needs a SIGN-OFF       -> approving the pull request
    parked because the HOST REFUSED to merge -> the pull request landing

Demanding an @-mention for the second is the deadlock this file exists to
prevent: the reviewer presses Approve, which posts no issue comment at all,
and the runner that knows how to read an approval is exactly the runner the
label stops from running.
"""

import pathlib
import unittest

import approval_release
import fakeforge
import forge
from fakeforge import note
from harness import load

SHA = "8beb9b5927aa11bb"
ASK = ("🛂 MERGE APPROVAL REQUESTED (sha `8beb9b59`)\n\n"
       "@someone — approve PR #94 to let it land.")
BLOCKED = ("🚧 MERGE BLOCKED (sha `8beb9b59`)\n\n"
           "Please merge PR #94 yourself.")
GUARD_ASK = "🛑 DESTRUCTIVE CHANGE — PLEASE CONFIRM\n\n@someone — I need clarification"


def approved(who="human", sha=SHA):
    return {"author": who, "verdict": "approved", "sha": sha}


def rejected(who="human", sha=SHA):
    return {"author": who, "verdict": "changes_requested", "sha": sha}


class ParkRelease(unittest.TestCase):
    def setUp(self):
        self.h = load("heartbeat-issue-tick")
        self.forge = fakeforge.FakeForge(identity="bot")
        self.h.FORGES = forge.Forges([self.forge])

    def release(self, notes, verdicts=(), labels=("On Hold",), pr_state=None):
        self.forge.notes[5] = list(notes)
        self.forge.verdicts[94] = list(verdicts)
        if pr_state is not None:
            self.forge.change_requests[94] = {"number": 94, "state": pr_state}
        issue = {"number": 5, "labels": list(labels)}
        return self.h.release_hold(self.forge, "o/r", issue, "bot")


class ApprovalPark(ParkRelease):
    """Parked for a sign-off. Approving IS the answer — no mention required."""

    def test_the_approve_button_alone_releases_it(self):
        # THE DEADLOCK. The button posts no issue comment, so a rule that
        # waits for a reply waits forever while the reviewer believes they
        # have already answered.
        self.assertTrue(self.release([note(ASK, "bot", 1)], [approved()]))

    def test_saying_lgtm_on_the_issue_releases_it(self):
        self.assertTrue(self.release(
            [note(ASK, "bot", 1), note("LGTM", "human", 2)]))

    def test_lgtm_typed_into_a_comment_review_releases_it(self):
        # The third way a person answers, and the one that fell through: the
        # review UI's "Comment" option. The verdict state is "commented", so
        # the button rule skips it, and the host keeps review bodies out of
        # the comments list, so the words rule never saw it either. Observed
        # live: a reviewer answered the ask with a review whose whole body was
        # "lgtm", and the park held for hours.
        self.assertTrue(self.release(
            [note(ASK, "bot", 1)],
            [{"author": "human", "verdict": "commented",
              "body": "lgtm", "sha": SHA}]))

    def test_a_comment_review_on_a_superseded_commit_is_not_a_sign_off(self):
        self.assertFalse(self.release(
            [note(ASK, "bot", 1)],
            [{"author": "human", "verdict": "commented",
              "body": "lgtm", "sha": "dead0000beef1111"}]))

    def test_a_comment_review_without_lgtm_is_feedback_not_a_sign_off(self):
        # "looks interesting" is conversation. Releasing on ANY commented
        # review would merge on small talk.
        self.assertFalse(self.release(
            [note(ASK, "bot", 1)],
            [{"author": "human", "verdict": "commented",
              "body": "looks interesting, why the extra flag?", "sha": SHA}]))

    def test_requesting_changes_releases_it_too(self):
        # A rejection ends the wait as surely as an approval: the reviewer has
        # handed the issue back. Staying parked would leave what they typed
        # unread — the same failure, pointed the other way.
        self.assertTrue(self.release([note(ASK, "bot", 1)], [rejected()]))

    def test_a_mention_still_works(self):
        self.assertTrue(self.release(
            [note(ASK, "bot", 1), note("@bot change the copy first", "human", 2)]))

    # -- what must NOT release it -----------------------------------------

    def test_the_bot_approving_its_own_pull_request_is_not_a_sign_off(self):
        self.assertFalse(self.release([note(ASK, "bot", 1)],
                                      [approved(who="bot")]))

    def test_an_approval_of_a_superseded_commit_is_not_a_sign_off(self):
        self.assertFalse(self.release([note(ASK, "bot", 1)],
                                      [approved(sha="dead0000beef1111")]))

    def test_lgtm_posted_before_the_ask_is_not_an_answer_to_it(self):
        self.assertFalse(self.release(
            [note("lgtm in advance", "human", 1), note(ASK, "bot", 2)]))

    def test_bystander_chatter_does_not_release_it(self):
        self.assertFalse(self.release(
            [note(ASK, "bot", 1), note("following this one", "human", 2)]))

    def test_an_unreadable_review_list_stays_parked(self):
        # Fails toward parked: a park that outlives its answer costs a reply.
        self.forge.raises = {"review_verdicts": RuntimeError("500")}
        self.assertFalse(self.release([note(ASK, "bot", 1)], [approved()]))

    def test_an_unreadable_review_list_still_honours_a_mention(self):
        # The pull-request branch returning "I don't know" must not swallow
        # the reply rule underneath it.
        self.forge.raises = {"review_verdicts": RuntimeError("500")}
        self.assertTrue(self.release(
            [note(ASK, "bot", 1), note("@bot go on", "human", 2)]))


class QuestionParkIsUnchanged(ParkRelease):
    """The destructive-work park keeps its higher bar. Nothing here loosens it."""

    def test_an_approval_elsewhere_does_not_release_a_guard_ask(self):
        self.assertFalse(self.release([note(GUARD_ASK, "bot", 1)], [approved()]))

    def test_lgtm_does_not_release_a_guard_ask(self):
        self.assertFalse(self.release(
            [note(GUARD_ASK, "bot", 1), note("lgtm", "human", 2)]))

    def test_a_mention_releases_a_guard_ask(self):
        self.assertTrue(self.release(
            [note(GUARD_ASK, "bot", 1), note("@bot yes, do it", "human", 2)]))

    def test_the_newest_ask_governs_when_both_are_on_the_issue(self):
        # Approval asked first, then the guard asked about a reworded issue.
        # The open question is the guard's, so an approval must not answer it.
        self.assertFalse(self.release(
            [note(ASK, "bot", 1), note(GUARD_ASK, "bot", 2)], [approved()]))


class MergeBlockedPark(ParkRelease):
    """Parked because the host refused the merge. Landing it ends the wait."""

    def test_the_pull_request_being_merged_releases_it(self):
        self.assertTrue(self.release([note(BLOCKED, "bot", 1)],
                                     pr_state="merged"))

    def test_an_open_pull_request_stays_parked(self):
        self.assertFalse(self.release([note(BLOCKED, "bot", 1)],
                                      pr_state="open"))

    def test_a_mention_releases_it(self):
        self.assertTrue(self.release(
            [note(BLOCKED, "bot", 1), note("@bot rebase it first", "human", 2)],
            pr_state="open"))

    def test_an_unreadable_pull_request_stays_parked(self):
        self.forge.raises = {"change_request": RuntimeError("500")}
        self.assertFalse(self.release([note(BLOCKED, "bot", 1)]))


class PullRequestBranchInIsolation(unittest.TestCase):
    """`_release_pr_park` returns None for "I don't know", not False.

    False would mean "not released" and would swallow the @-mention rule
    underneath it — so an unreadable pull request would silently disable the
    reply path as well.
    """

    def setUp(self):
        self.h = load("heartbeat-issue-tick")
        self.forge = fakeforge.FakeForge(identity="bot")

    def test_an_ask_without_a_pull_request_number_defers(self):
        self.assertIsNone(self.h._release_pr_park(
            self.forge, "o/r", 5, "On Hold", "approval",
            {"id": 1, "pr": None, "sha": SHA}, [], "bot"))

    def test_no_verdict_yet_defers(self):
        self.forge.verdicts[94] = []
        self.assertIsNone(self.h._release_pr_park(
            self.forge, "o/r", 5, "On Hold", "approval",
            {"id": 1, "pr": 94, "sha": SHA}, [], "bot"))

    def test_a_label_that_will_not_come_off_is_reported_as_not_released(self):
        self.forge.verdicts[94] = [approved()]
        self.forge.writes_fail = True
        self.assertIs(self.h._release_pr_park(
            self.forge, "o/r", 5, "On Hold", "approval",
            {"id": 1, "pr": 94, "sha": SHA}, [], "bot"), False)


class SignOffPredicate(unittest.TestCase):
    """`approval_release` is imported by the planner AND the solver's gate.

    They must not be able to disagree: the planner takes the label off and the
    gate decides whether to merge, so a rule that released on something the
    gate refuses hands the issue to a runner that re-parks it, every tick.
    """

    def test_lgtm_inside_a_link_is_not_a_sign_off(self):
        self.assertIsNone(approval_release.signed_off(
            comments=[note("see https://ci/lgtm-report", "human", 2)],
            bot="bot", anchor_id=1))

    def test_lgtm_inside_a_code_block_is_not_a_sign_off(self):
        self.assertIsNone(approval_release.signed_off(
            comments=[note("```\nlgtm\n```", "human", 2)],
            bot="bot", anchor_id=1))

    def test_a_quoted_lgtm_is_not_a_sign_off(self):
        self.assertIsNone(approval_release.signed_off(
            comments=[note("> LGTM\n\nthat was about the other PR", "human", 2)],
            bot="bot", anchor_id=1))

    def test_an_abbreviated_sha_matches_the_full_one(self):
        # The ask quotes 8 characters, a verdict carries 40. Comparing them
        # with == can only ever be false, so no approval would be recognised.
        self.assertTrue(approval_release.sha_match("8beb9b59", SHA))
        self.assertFalse(approval_release.sha_match("8beb9b59", "dead0000"))

    def test_too_short_to_identify_a_commit_never_matches(self):
        self.assertFalse(approval_release.sha_match("8beb", "8beb9b59"))

    def test_an_approval_and_a_rejection_are_never_confused(self):
        self.assertIsNone(approval_release.signed_off(
            verdicts=[rejected()], bot="bot", sha=SHA))
        self.assertIsNone(approval_release.changes_requested(
            verdicts=[approved()], bot="bot", sha=SHA))


class ReadingTheAsk(unittest.TestCase):
    """Which park an issue is in is read off the bot's own note.

    No extra state file, and nothing that can drift out of step with the
    labels: the ask the solver posted IS the record of what it is waiting for.
    """

    def test_approval_ask_finds_the_pull_request_and_the_commit(self):
        got = approval_release.approval_ask([note(ASK, "bot", 7)], "bot")
        self.assertEqual(got, {"id": 7, "pr": 94, "sha": "8beb9b59"})

    def test_merge_blocked_ask_is_a_different_wait(self):
        self.assertIsNone(approval_release.merge_blocked_ask(
            [note(ASK, "bot", 7)], "bot"))
        self.assertEqual(
            approval_release.merge_blocked_ask([note(BLOCKED, "bot", 7)],
                                               "bot")["pr"], 94)

    def test_an_ask_somebody_else_posted_is_not_the_bots_wait(self):
        # Anyone can quote the bot. Only the bot parks the issue.
        self.assertIsNone(approval_release.approval_ask(
            [note(ASK, "impostor", 7)], "bot"))

    def test_the_newest_ask_wins_when_the_solver_asked_twice(self):
        # A push re-asks, and the sign-off has to be about the commit named by
        # the ask that is still open.
        newer = ASK.replace("8beb9b59", "ffff0000")
        got = approval_release.approval_ask(
            [note(ASK, "bot", 1), note(newer, "bot", 9)], "bot")
        self.assertEqual(got["sha"], "ffff0000")

    def test_newest_park_ask_names_the_kind(self):
        self.assertEqual(
            approval_release.newest_park_ask(
                [note(ASK, "bot", 1), note(BLOCKED, "bot", 2)], "bot")[0],
            "blocked")
        self.assertEqual(
            approval_release.newest_park_ask(
                [note(BLOCKED, "bot", 1), note(ASK, "bot", 2)], "bot")[0],
            "approval")

    def test_newest_park_ask_says_nothing_when_the_bot_never_asked(self):
        self.assertEqual(approval_release.newest_park_ask([], "bot"),
                         (None, None))


class UnwrappingWhatTheForgeReturned(unittest.TestCase):
    """One fact, three shapes. GitHub nests the login under `author.login`,
    GitLab under `author.username`, and a review verdict carries it bare."""

    def test_login_reads_either_nesting(self):
        self.assertEqual(approval_release._login({"author": {"login": "a"}}), "a")
        self.assertEqual(approval_release._login({"author": {"username": "b"}}), "b")

    def test_login_of_something_that_is_not_a_note_is_empty(self):
        self.assertEqual(approval_release._login(None), "")
        self.assertEqual(approval_release._login({}), "")

    def test_author_folds_the_login_for_comparison(self):
        self.assertEqual(approval_release._author({"author": {"login": "Bot"}}),
                         "bot")

    def test_norm_folds_and_trims(self):
        self.assertEqual(approval_release._norm("  Bot  "), "bot")
        self.assertEqual(approval_release._norm(None), "")

    def test_newest_ignores_system_notes(self):
        # A forge's own timeline events are not the bot speaking.
        rows = [dict(note(ASK, "bot", 3), system=True)]
        self.assertIsNone(approval_release._newest(rows, "bot",
                                                   approval_release._ASK))

    def test_prose_removes_what_the_author_did_not_say(self):
        self.assertNotIn("lgtm", approval_release._prose("`lgtm`").lower())
        self.assertNotIn("lgtm", approval_release._prose("> lgtm").lower())
        self.assertNotIn("lgtm", approval_release._prose("http://x/lgtm").lower())
        self.assertIn("lgtm", approval_release._prose("lgtm").lower())


class NoInvisibleParks(unittest.TestCase):
    """The marker may never be written without the label that shows it.

    The marker lives on a volume only the pod can read, and it ranks the issue
    LAST — which in a repo with a dozen open issues means never. Setting it
    without labelling the issue is a park nobody outside the cluster can see:
    the issue still reads `status::in-progress` and simply stops moving. That
    is how #86 sat for hours. `park_on_hold` writes both; nothing else may
    write the marker.
    """

    def test_every_wait_on_a_person_is_labelled_before_it_exits(self):
        """A branch that exits because it is waiting must SAY so on the issue.

        Writing the marker is not enough and never was: it lives on a volume
        only the pod can read, so a wait recorded only there is invisible from
        GitHub while the issue still reads as ordinary open work. Worse, the
        branch runs every five minutes and burns the repository's one slot on
        an issue it will not touch — so an invisible park does not merely hide,
        it starves.

        k8s-ultimate-web-stack#113: asked for clarification, marker written,
        no label, 28 hours of spawning a runner that exited silently.
        """
        import pathlib as _pl
        src = (_pl.Path(__file__).resolve().parents[1] / "fixer-runner.sh")
        lines = src.read_text().splitlines()
        for n, line in enumerate(lines):
            if "exiting silently (waiting for user reply)" not in line:
                continue
            # park_on_hold must run BEFORE the exit, in the same branch.
            before = "\n".join(lines[max(0, n - 14):n])
            self.assertIn("park_on_hold", before,
                          f"line {n + 1} waits on a person without labelling "
                          "the issue — the park is invisible")
            break
        else:
            self.fail("the lexical silent-exit branch has moved or gone; "
                      "check it still labels before exiting")

    def test_the_guard_reads_history_not_just_the_local_marker(self):
        """Both dead ends must consult lexical_ask_answered.

        The marker-and-cursor pair deadlocks on its own: escaping "asked, not
        answered" needs a mention NEWER than the cursor, but the cursor
        advances past the answer the first time it is read. So the issue parks
        every tick forever (#116: 82 label events in twelve hours), and
        clearing the marker by hand makes it worse — the guard then sees a
        fresh issue and posts the ask a second time.

        The issue's own history cannot drift, so both the re-ask gate and the
        silent-exit branch ask it whether a person already answered.
        """
        import pathlib as _pl
        src = (_pl.Path(__file__).resolve().parents[1] / "fixer-runner.sh")
        text = src.read_text()
        self.assertIn("lexical_ask_answered()", text,
                      "the history-based predicate is gone")
        lines = text.splitlines()

        # 1. the re-ask gate
        for n, line in enumerate(lines):
            if "destructive pattern matched" in line and "posting ASK" in line:
                gate = "\n".join(lines[max(0, n - 14):n])
                self.assertIn("lexical_ask_answered", gate,
                              f"line {n + 1} posts an ask without checking "
                              "whether one was already answered")
                break
        else:
            self.fail("the ask-posting branch has moved or gone")

        # 2. the silent-exit branch
        for n, line in enumerate(lines):
            if "exiting silently (waiting for user reply)" in line:
                branch = "\n".join(lines[max(0, n - 20):n])
                self.assertIn("lexical_ask_answered", branch,
                              f"line {n + 1} parks without checking the "
                              "issue history — the cursor alone deadlocks")
                break
        else:
            self.fail("the silent-exit branch has moved or gone")

    def test_an_answered_lexical_ask_is_retired_not_re_asked_forever(self):
        """The ask marker must be cleared in the branch that consumes the reply.

        The marker outliving its answer is a perpetual re-park: the cursor
        advances past the reply, the next tick finds the marker present with
        no NEWER mention, and parks again — so every @-mention buys exactly
        one run and the issue oscillates between parked and working every five
        minutes. k8s-ultimate-web-stack#116 logged 82 label events over twelve
        hours that way, the planner releasing and this branch re-parking.

        Retiring it is safe: the guard exists so the agent never sees a
        destructive issue body nobody confirmed. Once confirmed it stays
        confirmed.
        """
        import pathlib as _pl
        src = (_pl.Path(__file__).resolve().parents[1] / "fixer-runner.sh")
        lines = src.read_text().splitlines()
        for n, line in enumerate(lines):
            if "user replied, proceeding with agent" not in line:
                continue
            window = "\n".join(lines[n:n + 18])
            self.assertIn("LEXICAL_ASKED_MARKER", window,
                          f"line {n + 1} consumes the reply without retiring "
                          "the ask — the next tick re-parks on a question "
                          "that has been answered")
            self.assertRegex(window, r"rm -f .*LEXICAL_ASKED_MARKER")
            break
        else:
            self.fail("the lexical proceed branch has moved or gone")

    def test_resuming_work_lifts_the_label_where_the_answer_is_consumed(self):
        """Both proceed-past-a-park branches must call unpark_on_hold.

        The planner's release_hold also lifts the label, but on its own
        five-minute cadence and only when the issue wins that tick's spawn
        budget — observed lagging the actual work by hours, with the bot
        mid-implementation on an issue still labelled "waiting on a person".
        The label is a statement to a HUMAN about who is being waited on; the
        process that consumes the answer is the one that must retract it.
        """
        import pathlib as _pl
        src = (_pl.Path(__file__).resolve().parents[1] / "fixer-runner.sh")
        text = src.read_text()
        lines = text.splitlines()
        for needle in ("user replied, proceeding with agent",
                       "clearing the awaiting-human park"):
            for n, line in enumerate(lines):
                if needle not in line:
                    continue
                # Generous window: an explanatory comment block may sit
                # between the decision and the call, and this asserts the call
                # is IN the branch, not that it is the next line.
                window = "\n".join(lines[n:n + 20])
                self.assertIn("unpark_on_hold", window,
                              f"the branch at line {n + 1} resumes work "
                              "without lifting On Hold — the label lies "
                              "until the planner happens to catch up")
                break
            else:
                self.fail(f"proceed branch {needle!r} has moved or gone")
        # And the function itself must retract BOTH halves of the park —
        # label and marker — for the same reason park_on_hold writes both.
        start = next(n for n, line in enumerate(lines)
                     if line.startswith("unpark_on_hold()"))
        end = next(n for n in range(start + 1, len(lines))
                   if lines[n] == "}")
        body = "\n".join(lines[start:end])
        self.assertIn("remove-label", body)
        self.assertIn("AWAITING_HUMAN_MARKER", body)

    def test_only_park_on_hold_creates_the_marker(self):
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1] / "fixer-runner.sh"
        lines = src.read_text().splitlines()
        touches = [n for n, line in enumerate(lines, 1)
                   if "touch" in line and "AWAITING_HUMAN_MARKER" in line]
        self.assertEqual(len(touches), 1,
                         f"the marker is touched at lines {touches}; every "
                         "park must go through park_on_hold so it is visible")
        start = next(n for n, line in enumerate(lines, 1)
                     if line.startswith("park_on_hold()"))
        end = next(n for n, line in enumerate(lines[start:], start + 1)
                   if line == "}")
        self.assertTrue(start < touches[0] < end,
                        "the only touch must be the one inside park_on_hold")


class TheRuleShips(unittest.TestCase):
    """`approval_release` reaches both processes, by both routes.

    The planner imports it at module scope, so a copy missing from the cron
    image takes the whole tick down — loud, at least. The solver imports it
    inside a snippet whose failure used to be swallowed, which was worse: an
    empty login reads as "nobody approved", so the bot would have waited
    forever on sign-offs that had already been given.
    """

    def setUp(self):
        self.builder = pathlib.Path(__file__).resolve().parents[1]

    def test_it_is_in_the_gateway_image(self):
        self.assertIn("approval_release.py",
                      (self.builder / "Dockerfile").read_text())

    def test_it_is_in_the_cron_image(self):
        self.assertIn("approval_release.py",
                      (self.builder / "cron" / "Dockerfile").read_text())

    def test_it_is_in_the_planner_configmap(self):
        # The planner half, because the cron pods mount only that one — and
        # the gateway mounts both, so the solver reaches it there too.
        text = (self.builder / "kustomization.yaml").read_text()
        planner = text.split("claw-scripts-planner", 1)[1]
        planner = planner.split("claw-scripts-runner", 1)[0]
        self.assertIn("approval_release.py=approval_release.py", planner)

    def test_the_solver_does_not_swallow_an_import_failure(self):
        # `2>/dev/null` on these snippets would turn a missing module into
        # "nobody has approved anything", silently and forever.
        src = (self.builder / "fixer-runner.sh").read_text()
        for fn in ("pr_human_approval", "pr_changes_requested"):
            body = src.split(f"{fn}() {{", 1)[1].split("\n}", 1)[0]
            self.assertIn("import approval_release", body)
            # Only the python snippet's own stderr matters here: the
            # `2>/dev/null` on the forge-cli fetches above it is deliberate,
            # since those fall back to an empty list by design.
            self.assertNotIn('\n" 2>/dev/null', body,
                             f"{fn} hides why the python snippet failed")


if __name__ == "__main__":
    unittest.main()

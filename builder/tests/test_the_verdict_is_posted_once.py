"""One review, one verdict comment.

WHAT WAS HAPPENING
On one change request both verdicts landed twice: two comments two seconds
apart, then another pair the same way — and each pair was BYTE-IDENTICAL,
11526 bytes and 8897 bytes. Identical rules out two reviews;
the per-repo lock in reviewer-runner rules out two runs. The wrapper was not
the second poster either: its "wrapper posted the … verdict from the summary
file" line appears nowhere in that run log. The agent ran the poster twice on
the same --body-file, once with the exec reported as failed while the note had
in fact landed, and once with nothing reported at all.

Nothing on the path was idempotent. The sha guard proved the verdict was about
the right commit and then posted it, as many times as it was asked to.

WHY THE CHECK IS SCOPED TO THE RUN
"A verdict for this sha already exists" is the wrong question. reviewer-runner
is explicit that a head stays re-reviewable once its state file is cleared,
and that an earlier run's verdict must never be read as this one's result —
that was its own bug, fixed separately. So the question here is narrower: has
THIS run already posted? Outside a run there is no window, and with no window
the check does not run at all.

EVERY FAILURE ENDS WITH THE VERDICT POSTED
A fetch that fails, a payload that will not parse, an unreadable window: all
of them fall through to posting. A verdict is the solver's merge gate, and
dropping one wedges the change request on nothing — strictly worse than the
duplicate this is here to prevent.
"""

import json
import os
import unittest
from datetime import datetime, timezone

from harness import ShellTestCase

SHA = "0ff22657dde67284a495f2ac2052eee7350b4cf1"
OTHER = "cf0e7d1ac1454ac94b6ee92abc031dd455c3f7bc"

MARKER = "\U0001f50e REVIEW RESULT:"
BOT = "claw-code-bot"

# The run's window. Fixtures are dated relative to this, so "during this run"
# and "before it" are unambiguous without depending on the clock.
RUN_START = 1788000000


def when(offset):
    return datetime.fromtimestamp(RUN_START + offset,
                                  timezone.utc).isoformat()


class RepeatTestCase(ShellTestCase):
    def setUp(self):
        super().setUp()
        self.env["FAKE_FORGE_DIR"] = "$PWD/fixtures"
        self.env["FAKE_FORGE_LOG"] = "$PWD/forge.log"
        self.fixtures = os.path.join(self.home, "fixtures")
        os.makedirs(self.fixtures, exist_ok=True)

    def already_on_the_change_request(self, *comments):
        """Canned answer for `change-request-comments`.

        No fixture at all is a different case on purpose — the fake forge
        fails the read, which is how "the host could not be asked" is tested.
        """
        with open(os.path.join(self.fixtures, "change-request-comments"),
                  "w", encoding="utf-8", newline="\n") as f:
            json.dump(list(comments), f)

    def verdict_comment(self, sha, offset=10, author=BOT,
                        result="CHANGES REQUIRED", backticks=False):
        head = f"{MARKER} {result} (sha `{sha}`)" if backticks \
            else f"{MARKER} {result} (sha {sha})"
        return {"id": 1, "body": head + "\n\nthe findings\n",
                "author": {"username": author},
                "createdAt": when(offset)}

    def verdict_body(self, sha=SHA, result="CHANGES REQUIRED"):
        return f"{MARKER} {result} (sha {sha})\n\nthe findings\n"

    def post(self, body, head=SHA, since=RUN_START, repo="o/r", number=60):
        setup = ""
        if head:
            setup += f"export REVIEW_HEAD_SHA={head}\n"
        if since is not None:
            setup += f"export REVIEW_RUN_START_EPOCH={since}\n"
        setup += f"export REVIEWER_BOT_LOGIN={BOT}\n"

        with open(os.path.join(self.home, "body.md"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(body)

        return self.sh(
            setup
            + f"review-verdict --repo {repo} --number {number}"
            + " --body-file body.md\n"
            + 'echo "rc=$?"')

    def posted(self):
        path = os.path.join(self.home, "forge.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]

    def comments_written(self):
        return [c for c in self.posted()
                if c.startswith("comment-on-change-request")]


class TheSecondCopyIsRefused(RepeatTestCase):
    def test_the_same_verdict_is_not_posted_twice(self):
        self.already_on_the_change_request(self.verdict_comment(SHA))
        rc, out, err = self.post(self.verdict_body())
        # Exit 0, not a failure: the verdict IS on the change request, which
        # is what the caller wanted. Failing here would tell the agent its
        # verdict did not land and invite exactly the retry that caused this.
        self.assertIn("rc=0", out, out + err)
        self.assertEqual(self.comments_written(), [], self.posted())

    def test_it_says_why_nothing_was_posted(self):
        self.already_on_the_change_request(self.verdict_comment(SHA))
        _, _, err = self.post(self.verdict_body())
        self.assertIn("already on", err)

    def test_a_backticked_header_is_recognised_as_the_same_verdict(self):
        # What a model actually writes. A check that only read the bare form
        # would find no sha on the posted copy, call it a different verdict
        # and let the duplicate through — the common case, silently unguarded.
        self.already_on_the_change_request(
            self.verdict_comment(SHA, backticks=True))
        rc, out, _ = self.post(self.verdict_body())
        self.assertIn("rc=0", out)
        self.assertEqual(self.comments_written(), [])

    def test_an_abbreviated_posted_sha_still_matches(self):
        # The wrapper writes forty characters, a model usually writes seven.
        self.already_on_the_change_request(self.verdict_comment(SHA[:7]))
        rc, out, _ = self.post(self.verdict_body())
        self.assertIn("rc=0", out)
        self.assertEqual(self.comments_written(), [])

    def test_the_window_may_come_from_the_state_file(self):
        # `_run_start_epoch`'s second source, and the one that matters most.
        # The agent posts from inside its own exec sandbox, which need not
        # carry the environment across — the same reason the sha guard reads
        # the state file. Losing the window there would turn this check off
        # precisely on the path the duplicates came from: reviewer-runner
        # writes the file when it commits to a head and deletes it on exit, so
        # its mtime is this run's start and it is absent between runs.
        key = "o__r__60"
        state = os.path.join(self.home, ".openclaw", "reviewer-state")
        os.makedirs(state, exist_ok=True)
        path = os.path.join(state, f"{key}.reviewing-sha")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(SHA)
        os.utime(path, (RUN_START, RUN_START))

        self.already_on_the_change_request(self.verdict_comment(SHA))
        rc, out, err = self.post(self.verdict_body(), head=None, since=None)
        self.assertIn("rc=0", out, out + err)
        self.assertEqual(self.comments_written(), [], self.posted())


class AVerdictThatIsNotARepeatStillPosts(RepeatTestCase):
    def test_an_empty_change_request_posts(self):
        self.already_on_the_change_request()
        rc, out, err = self.post(self.verdict_body())
        self.assertIn("rc=0", out, out + err)
        self.assertTrue(self.comments_written(), self.posted())

    def test_an_earlier_run_s_verdict_does_not_suppress_this_one(self):
        # The re-review case, and the reason the window exists. A head stays
        # re-reviewable once its state file is cleared; keying on "a verdict
        # for this sha exists" would make the second review permanently
        # silent, which is how a head becomes unreviewable forever.
        self.already_on_the_change_request(self.verdict_comment(SHA, offset=-3600))
        rc, out, _ = self.post(self.verdict_body())
        self.assertIn("rc=0", out)
        self.assertTrue(self.comments_written())

    def test_a_verdict_about_another_commit_does_not_suppress_it(self):
        self.already_on_the_change_request(self.verdict_comment(OTHER))
        rc, out, _ = self.post(self.verdict_body())
        self.assertIn("rc=0", out)
        self.assertTrue(self.comments_written())

    def test_somebody_else_s_comment_is_not_this_run_s_verdict(self):
        self.already_on_the_change_request(
            self.verdict_comment(SHA, author="a-person"))
        rc, out, _ = self.post(self.verdict_body())
        self.assertIn("rc=0", out)
        self.assertTrue(self.comments_written())

    def test_an_ordinary_comment_on_the_change_request_is_not_a_verdict(self):
        self.already_on_the_change_request(
            {"id": 2, "body": f"looks good, {SHA} is the head",
             "author": {"username": BOT}, "createdAt": when(5)})
        rc, out, _ = self.post(self.verdict_body())
        self.assertIn("rc=0", out)
        self.assertTrue(self.comments_written())

    def test_an_ordinary_comment_is_never_checked_at_all(self):
        # Only a verdict header reaches the check. Everything else the
        # reviewer says goes straight out, and repeats are none of its
        # business.
        self.already_on_the_change_request(
            {"id": 3, "body": "a note\n", "author": {"username": BOT},
             "createdAt": when(5)})
        rc, out, _ = self.post("a note\n")
        self.assertIn("rc=0", out)
        self.assertTrue(self.comments_written())
        self.assertFalse(
            any(c.startswith("change-request-comments") for c in self.posted()),
            "an ordinary comment should not cost a read of the whole thread")


class WhenItCannotTellItPosts(RepeatTestCase):
    """Every way this check can fail ends with the verdict on the change
    request. A duplicate is noise; a dropped verdict is a merge gate with
    nothing behind it."""

    def test_a_failed_read_posts(self):
        # No fixture: the fake forge fails the read exactly as the real
        # command does for a question it cannot answer.
        rc, out, err = self.post(self.verdict_body())
        self.assertIn("rc=0", out, out + err)
        self.assertTrue(self.comments_written(), self.posted())

    def test_a_payload_that_is_not_a_list_posts(self):
        with open(os.path.join(self.fixtures, "change-request-comments"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write('{"message": "404 Not Found"}')
        rc, out, _ = self.post(self.verdict_body())
        self.assertIn("rc=0", out)
        self.assertTrue(self.comments_written())

    def test_unparseable_output_posts(self):
        with open(os.path.join(self.fixtures, "change-request-comments"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write("not json at all")
        rc, out, _ = self.post(self.verdict_body())
        self.assertIn("rc=0", out)
        self.assertTrue(self.comments_written())

    def test_no_run_window_means_no_opinion(self):
        # A verdict posted outside a run — no environment, no state file. The
        # check has nothing to define "this run" with, so it does not run.
        self.already_on_the_change_request(self.verdict_comment(SHA))
        rc, out, _ = self.post(self.verdict_body(), head=None, since=None)
        self.assertIn("rc=0", out)
        self.assertTrue(self.comments_written())

    def test_a_malformed_window_means_no_opinion(self):
        self.already_on_the_change_request(self.verdict_comment(SHA))
        rc, out, _ = self.post(self.verdict_body(), since="not-a-number")
        self.assertIn("rc=0", out)
        self.assertTrue(self.comments_written())


class TheOtherGuardStillHolds(RepeatTestCase):
    """The repeat check is added BESIDE the sha guard, not in front of it. A
    verdict about another commit must still be refused, and refused before
    anything is read or posted."""

    def test_a_verdict_about_another_commit_is_still_refused(self):
        self.already_on_the_change_request()
        rc, out, err = self.post(self.verdict_body(sha=OTHER))
        self.assertIn("rc=1", out, out + err)
        self.assertIn("REFUSED", err)
        self.assertEqual(self.posted(), [], self.posted())


if __name__ == "__main__":
    unittest.main()

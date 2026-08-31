"""One request note per head, and the head is what unlocks the next one.

WHAT WAS HAPPENING
On one stalled change request the solver posted

    🔎 Requested an autonomous review of `0ff22657` — …

NINE times for the same commit: 20:29, 22:30, 00:30, 02:35, 04:40, 06:40,
08:45, 10:50, 12:50. One every two hours, which is REVIEW_WAIT_TTL. The
reviewer was meanwhile failing on an exhausted model quota, so no verdict was
coming, and past the TTL request_self_review took that as licence to "ask
again rather than wait on a promise nobody is left to keep".

But there is nobody to ask. Read the paragraph the function opens with: no
reviewer is requested — the host refuses the change's own author — and the
reviewer finds the work by AUTHORSHIP. The note is for the person reading the
pull request. So re-posting it moved nothing on the reviewer's side, and the
nine copies buried the one that was worth reading.

WHAT THE NOTE MEANS, WHICH IS WHY ONE IS ENOUGH
Its presence says: the solver is finished with this head, the pipelines are
green, it is waiting. That is a fact about a COMMIT. It does not become more
true, or newer, by being said again — only the head moving makes it a
different statement.

THE HEAD IS THE PIN
A new push is a new head, and a new head gets its own note, immediately, with
no TTL involved: the solver has finished with a different commit and is
waiting on a different verdict. That is the case these tests spend the most
assertions on, because it is the one a repeat-suppression bug would break —
and a solver that could no longer ask for a review of its new work would be a
far worse failure than nine notes.

The TTL is kept, and now says only what it can back up: the wait is old. The
planner reads the same marker and the same TTL when it ranks, and that is
untouched — the marker's mtime is that reading, so this path deliberately
does not refresh it.
"""

import os
import unittest

from harness import BUILDER, ShellTestCase

SHA = "0ff22657dde67284a495f2ac2052eee7350b4cf1"
NEXT = "cf0e7d1ac1454ac94b6ee92abc031dd455c3f7bc"

TTL = 7200


def func(name: str) -> str:
    """The real lines out of fixer-runner.sh, never a copy of them."""
    with open(os.path.join(BUILDER, "fixer-runner.sh"), encoding="utf-8") as f:
        s = f.read()
    start = s.index(f"{name}() {{")
    return s[start:s.index("\n}", start) + 2]


class NoteTestCase(ShellTestCase):
    """Drives request_self_review with the forge replaced by a log.

    Only three things from the 2000-line runner are needed: where the marker
    lives, how long the wait is believed, and something to post with. Anything
    else the function reaches for is a dependency it should not have.
    """

    def run_request(self, sha, marker=None, age=None, ttl=TTL):
        """Ask for a review of `sha`, with the marker in a given state.

        `marker` is what a previous call recorded; `age` is how long ago, so a
        test can put the wait either side of the TTL.
        """
        path = os.path.join(self.home, "marker")
        if marker is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(marker)
            if age is not None:
                stamp = os.stat(path).st_mtime - age
                os.utime(path, (stamp, stamp))
        # What the marker looked like going in, so a test can assert the call
        # left it alone.
        self.mtime_before = (os.stat(path).st_mtime
                             if os.path.exists(path) else None)

        rc, out, err = self.sh(
            'AWAITING_REVIEW_MARKER="$PWD/marker"\n'
            f"REVIEW_WAIT_TTL={ttl}\n"
            # The forge, reduced to the one thing under test: what got said.
            'post_pr_comment() { printf "%s\\n" "$2" >> "$PWD/posted.log"; }\n'
            # Newline-joined: func() ends at the closing brace, so running
            # them together would put the next definition on the same line.
            + "\n".join((func("review_wait_expired"),
                         func("review_wait_age"),
                         func("request_self_review")))
            + f"\nrequest_self_review 60 {sha}\n"
            + 'echo "rc=$?"')
        self.assertIn("rc=0", out, out + err)
        return out

    def notes(self):
        path = os.path.join(self.home, "posted.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]

    def marker(self):
        path = os.path.join(self.home, "marker")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read().strip()

    def marker_mtime(self):
        return os.stat(os.path.join(self.home, "marker")).st_mtime


class TheFirstAskIsPosted(NoteTestCase):
    def test_a_head_with_no_marker_gets_a_note(self):
        self.run_request(SHA)
        self.assertEqual(len(self.notes()), 1, self.notes())

    def test_the_note_names_the_head_it_is_about(self):
        self.run_request(SHA)
        self.assertIn(SHA[:8], self.notes()[0])

    def test_the_head_is_recorded(self):
        self.run_request(SHA)
        self.assertEqual(self.marker(), SHA)


class ANewHeadIsANewAsk(NoteTestCase):
    """The head is the pin. A push means the solver is waiting on a different
    commit, so it says so — at once, whatever the previous wait was doing."""

    def test_a_new_head_posts_its_own_note(self):
        self.run_request(NEXT, marker=SHA, age=1)
        self.assertEqual(len(self.notes()), 1, self.notes())
        self.assertIn(NEXT[:8], self.notes()[0])

    def test_a_new_head_does_not_wait_for_the_ttl(self):
        # The previous wait is one second old — as fresh as it gets. It has no
        # bearing on a note about a commit it was never about.
        self.run_request(NEXT, marker=SHA, age=1, ttl=999999)
        self.assertEqual(len(self.notes()), 1)

    def test_the_new_head_replaces_the_recorded_one(self):
        self.run_request(NEXT, marker=SHA, age=1)
        self.assertEqual(self.marker(), NEXT)

    def test_a_head_that_moved_and_came_back_asks_again(self):
        # Reverted to a commit whose note has since been superseded. The
        # marker names the other head, so this is a new statement.
        self.run_request(SHA, marker=NEXT, age=1)
        self.assertEqual(len(self.notes()), 1)


class TheSameHeadIsNotAskedTwice(NoteTestCase):
    def test_a_fresh_wait_posts_nothing(self):
        self.run_request(SHA, marker=SHA, age=60)
        self.assertEqual(self.notes(), [])

    def test_an_expired_wait_posts_nothing_either(self):
        # THE REGRESSION. Past the TTL this used to post the note again, and
        # again every two hours, for as long as the reviewer stayed stuck.
        self.run_request(SHA, marker=SHA, age=TTL + 600)
        self.assertEqual(self.notes(), [], self.notes())

    def test_a_wait_far_past_the_ttl_still_posts_nothing(self):
        # Sixteen hours, which is what !60 actually sat through.
        self.run_request(SHA, marker=SHA, age=16 * 3600)
        self.assertEqual(self.notes(), [])

    def test_the_stale_wait_is_reported_in_the_log_instead(self):
        # Not silence: whoever is looking into the stall needs to see it. The
        # log is where that belongs — it costs no comment on the change
        # request and nobody has to scroll past it.
        out = self.run_request(SHA, marker=SHA, age=TTL + 600)
        self.assertIn("pending", out)
        self.assertIn(SHA[:8], out)

    def test_the_log_reports_the_real_age(self):
        out = self.run_request(SHA, marker=SHA, age=TTL + 600)
        self.assertRegex(out, r"pending for 7[0-9]{3}s")

    def test_the_marker_is_not_refreshed(self):
        # The planner ranks on this mtime and the TTL reading is derived from
        # it. Touching it here would reset the age on every tick, so a wait
        # that never ends would never look old to anything.
        self.run_request(SHA, marker=SHA, age=TTL + 600)
        self.assertEqual(self.marker(), SHA)
        self.assertAlmostEqual(self.mtime_before, self.marker_mtime(), delta=1,
                               msg="the stale wait was refreshed, so it can "
                                   "never look stale to the planner")


class TheTtlStillMeansSomething(NoteTestCase):
    def test_expiry_is_decided_by_the_ttl_not_a_constant(self):
        # A deployment that sets REVIEW_WAIT_TTL gets its own answer. With a
        # tiny TTL the wait reads as stale immediately — and still posts
        # nothing, which is the whole point.
        out = self.run_request(SHA, marker=SHA, age=5, ttl=1)
        self.assertIn("pending", out)
        self.assertEqual(self.notes(), [])

    def test_within_the_ttl_it_says_it_is_waiting(self):
        out = self.run_request(SHA, marker=SHA, age=60)
        self.assertIn("already requested", out)


if __name__ == "__main__":
    unittest.main()

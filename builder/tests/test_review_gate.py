"""What the reviewer planner decides, and why it may not get it wrong.

THE DECISION THIS PINS
----------------------
**Has this already been reviewed?** Keyed on the head SHA alone, a verdict
about the DESCRIPTION could never be cleared: the author edits the body, the
SHA does not move, nothing looks again, and the change request sits until a
human intervenes. The record therefore carries the SHA *and* a digest of the
prose — see review_subject.py.

The other decision the planner gates on — **is the head green?** — is asked of
the forge and answered there, because the reduction is host-specific and must
have exactly one definition. Its tests live with the implementations, in
test_forge_github.py and test_forge_gitlab.py.

No network: the planner reaches its host only through the forge interface.
"""

import unittest

from harness import load, load_script

import forge  # noqa: E402 - harness puts builder/ on sys.path first

rt = load_script("reviewer-tick.py")
rs = load("review_subject")


class AlreadyReviewed(unittest.TestCase):
    """The fingerprint that lets an edited description clear a verdict."""

    SHA = "c6ca71f0000000000000000000000000000000aa"

    def stamp(self, title, body, sha=None):
        return rs.stamp(sha or self.SHA, title, body)

    def test_the_same_commit_and_prose_is_skipped(self):
        stored = self.stamp("Add versions endpoint", "Closes #5")
        self.assertTrue(rs.already_reviewed(
            stored, self.SHA, "Add versions endpoint", "Closes #5"))

    def test_editing_the_DESCRIPTION_clears_the_verdict(self):
        # The regression this module exists for. The reviewer asked for the
        # "Closes #5" line to go; the author removed it; the SHA did not move.
        # Keyed on the SHA alone nothing looked again, and both sides waited
        # correctly while nothing moved.
        stored = self.stamp("Add versions endpoint", "Closes #5")
        self.assertFalse(rs.already_reviewed(
            stored, self.SHA, "Add versions endpoint", "part of #5"))

    def test_editing_the_TITLE_clears_it_too(self):
        stored = self.stamp("Add versions endpoint", "Closes #5")
        self.assertFalse(rs.already_reviewed(
            stored, self.SHA, "Add /api/version", "Closes #5"))

    def test_a_new_commit_clears_it(self):
        stored = self.stamp("t", "b")
        self.assertFalse(rs.already_reviewed(stored, "deadbeef", "t", "b"))

    def test_invisible_whitespace_does_NOT_clear_it(self):
        # The web editor and the API disagree about trailing newlines and
        # CRLF. Re-reviewing on that would loop forever.
        stored = self.stamp("t", "line one\nline two")
        self.assertTrue(rs.already_reviewed(
            stored, self.SHA, "t ", "line one\r\nline two\n"))

    def test_a_legacy_bare_sha_earns_exactly_one_re_review(self):
        # State written before the digest existed. Fails towards reviewing.
        self.assertFalse(rs.already_reviewed(self.SHA, self.SHA, "t", "b"))
        self.assertTrue(rs.already_reviewed(
            self.stamp("t", "b"), self.SHA, "t", "b"))

    def test_nothing_recorded_means_review_it(self):
        self.assertFalse(rs.already_reviewed(None, self.SHA, "t", "b"))
        self.assertFalse(rs.already_reviewed("", self.SHA, "t", "b"))

    def test_the_planner_uses_this_module_not_a_copy(self):
        # Two implementations of the digest would mean every pull request
        # looked changed forever. (Same file, not the same object: the
        # harness re-imports modules fresh for each test module.)
        self.assertEqual(rt.review_subject.__file__, rs.__file__)
        self.assertEqual(rt.review_subject.fingerprint("t", "b"),
                         rs.fingerprint("t", "b"))


class Discovery(unittest.TestCase):
    """What the planner asks for, and what it does with what comes back.

    WHICH host answers, and how it finds the bot's own work, is the forge's
    problem — see test_forge_github.py for why authorship rather than a review
    request is the primary signal there. What the PLANNER owes is narrower and
    tested here: it must not go looking at all when it does not know who it is.
    """

    def setUp(self):
        self._real = rt.FORGES
        self.calls = []

    def tearDown(self):
        rt.FORGES = self._real

    def _forges(self, items):
        calls = self.calls

        class _Recording(forge.Forges):
            def reviewable_change_requests(self, limit):
                calls.append(limit)
                return list(items)

        rt.FORGES = _Recording([])
        return rt.FORGES

    def test_the_candidates_come_back_as_the_planner_sorts_them(self):
        self._forges([{"forge": forge.GITHUB, "repo": "o/r", "number": 1,
                       "labels": []}])
        items = rt.list_reviewable_prs("cameron-claw")
        self.assertEqual([i["number"] for i in items], [1])
        self.assertEqual(self.calls, [rt.MAX_PRS])

    def test_no_login_means_no_search_at_all(self):
        # Reviewing as nobody would pick up every open change request in every
        # permitted project.
        self._forges([{"forge": forge.GITHUB, "repo": "o/r", "number": 1}])
        self.assertEqual(rt.list_reviewable_prs(""), [])
        self.assertEqual(self.calls, [])

    def test_the_repo_comes_off_the_record_itself(self):
        self.assertEqual(rt.repo_of({"repo": "o/r", "number": 1}), "o/r")

    def test_a_malformed_item_names_no_repo(self):
        self.assertEqual(rt.repo_of({}), "")
        self.assertEqual(rt.repo_of(None), "")


class Permissions(unittest.TestCase):
    """`project-allow check`: exit 2 is an answer, anything else is not."""

    def test_zero_is_permitted(self):
        self.assertEqual(rt.permission_reason(0), "")

    def test_two_is_not_permitted(self):
        self.assertEqual(rt.permission_reason(2), "not-permitted")

    def test_anything_else_fails_closed(self):
        # The CLI missing, the list unreadable, the exec failing. We cannot
        # tell what is permitted, which permits nothing.
        for code in (1, 3, 127, None):
            with self.subTest(code=code):
                self.assertEqual(rt.permission_reason(code),
                                 "allowlist-unavailable")


if __name__ == "__main__":
    unittest.main()

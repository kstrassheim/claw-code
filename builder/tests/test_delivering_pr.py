"""Which pull request delivered an issue.

The failure this guards against is not a crash. It is a confident, plausible,
wrong `mergedAt` — and because every downstream report is built on that field,
one wrong value is wrong in all of them at once and looks right in each.
"""

import unittest

from harness import load


def pr(number, *, body="", branch=None, merged_at=None, merged=None):
    d = {"number": number, "body": body}
    if branch is not None:
        d["head"] = {"ref": branch}
    if merged_at is not None:
        d["merged_at"] = merged_at
    if merged is not None:
        d["merged"] = merged
    return d


class ClosingKeywords(unittest.TestCase):
    def setUp(self):
        self.d = load("delivering_pr")

    def test_every_platform_keyword_and_inflection(self):
        for word in ("close", "closes", "closed", "closing",
                     "fix", "fixes", "fixed", "fixing",
                     "resolve", "resolves", "resolved", "resolving"):
            with self.subTest(word=word):
                self.assertEqual(
                    self.d.closed_issues(f"{word} #63"), {"63"})

    def test_a_colon_after_the_keyword_is_allowed(self):
        self.assertEqual(self.d.closed_issues("Closes: #63"), {"63"})

    def test_words_the_platform_ignores_are_ignored_here(self):
        # Recording these as deliveries is the exact error this module
        # exists to prevent: they read like delivery to a person and mean
        # nothing to the platform, so the issue is never actually closed.
        for word in ("implements", "addresses", "relates to", "see"):
            with self.subTest(word=word):
                self.assertEqual(self.d.closed_issues(f"{word} #63"), set())

    def test_a_mention_is_not_a_closure(self):
        self.assertEqual(
            self.d.closed_issues("Unlike #63, this one is scoped"), set())

    def test_prose_after_a_keyword_does_not_swallow_a_later_mention(self):
        # "Closes the gap ... discussed in #63" must not read as closing 63.
        # This is why references are consumed from the keyword rather than
        # searched for anywhere in the body.
        self.assertEqual(
            self.d.closed_issues("Closes the gap we discussed in #63"), set())

    def test_a_list_after_one_keyword_closes_all_of_them(self):
        self.assertEqual(
            self.d.closed_issues("Fixes #63, #64 and #65"),
            {"63", "64", "65"})

    def test_the_issue_word_is_tolerated(self):
        self.assertEqual(self.d.closed_issues("Closes issue #63"), {"63"})

    def test_a_cross_repository_reference_does_not_close_ours(self):
        # Two repositories both having an issue 12 is ordinary. Without this
        # they contaminate each other's delivery records.
        self.assertEqual(
            self.d.closed_issues("Closes other/project#12", repo="me/mine"),
            set())

    def test_an_explicit_reference_to_our_own_repo_does_close(self):
        self.assertEqual(
            self.d.closed_issues("Closes me/mine#12", repo="me/mine"),
            {"12"})


class MergedMeansMerged(unittest.TestCase):
    def setUp(self):
        self.d = load("delivering_pr")

    def test_an_abandoned_pull_request_is_not_a_delivery(self):
        # Closed without merging. Counting this is the same class of error as
        # counting a mention as a closure.
        self.assertFalse(self.d.is_merged({"state": "closed"}))

    def test_a_merge_timestamp_means_merged(self):
        self.assertTrue(self.d.is_merged({"merged_at": "2026-08-09T10:00:00Z"}))

    def test_the_merged_flag_means_merged(self):
        self.assertTrue(self.d.is_merged({"merged": True}))


class PickingTheDeliverer(unittest.TestCase):
    def setUp(self):
        self.d = load("delivering_pr")

    def test_the_runners_own_branch_needs_no_text(self):
        # The ordinary case: the wrapper knows which branch it worked the
        # issue on, so no keyword has to have been written correctly.
        p = pr(7, branch="issue-63-fix", merged_at="2026-08-09T10:00:00Z")
        self.assertIs(self.d.pick([p], 63, branch="issue-63-fix"), p)

    def test_nothing_qualifying_returns_none(self):
        self.assertIsNone(
            self.d.pick([pr(1, body="Mentions #63", merged_at="2026-08-09T10:00:00Z")], 63))

    def test_an_open_pull_request_never_delivers(self):
        self.assertIsNone(self.d.pick([pr(132, body="Closes #63")], 63))

    def test_the_newest_merge_wins_not_the_last_element(self):
        # Ordering is not documented as chronological, so "the last one" is a
        # coin flip that looks like a rule.
        old = pr(1, body="Closes #63", merged_at="2026-02-11T09:00:00Z")
        new = pr(9, body="Closes #63", merged_at="2026-08-09T10:00:00Z")
        self.assertIs(self.d.pick([new, old], 63), new)
        self.assertIs(self.d.pick([old, new], 63), new)

    def test_a_merge_predating_the_issue_cannot_have_delivered_it(self):
        ancient = pr(1, body="Closes #63", merged_at="2025-02-11T09:00:00Z")
        self.assertIsNone(
            self.d.pick([ancient], 63, not_before="2026-08-01T00:00:00Z"))

    def test_the_full_failing_shape(self):
        # Three merged pull requests that merely mention the issue, one from
        # eighteen months before it existed, and the real one still open.
        # The answer must be "nothing delivered this yet".
        candidates = [
            pr(1, body="Permit site access", merged_at="2025-02-11T09:00:00Z"),
            pr(132, body="Closes #63"),
            pr(137, body="Test setup: silence noise, see #63",
               merged_at="2026-08-09T08:00:00Z"),
            pr(138, body="Fix publish step (#63 adjacent)",
               merged_at="2026-08-09T09:00:00Z"),
        ]
        self.assertIsNone(
            self.d.pick(candidates, 63, branch="issue-63-fix",
                        not_before="2026-08-01T00:00:00Z"))

    def test_offset_timestamps_compare_as_instants_not_as_text(self):
        # 14:30+02:00 IS 12:30Z — earlier than 13:00Z, though it sorts after
        # it as a string. Comparing text would be right in UTC CI and wrong
        # on a project in Zurich.
        earlier = pr(1, body="Closes #63", merged_at="2026-08-09T14:30:00+02:00")
        later = pr(2, body="Closes #63", merged_at="2026-08-09T13:00:00Z")
        self.assertIs(self.d.pick([earlier, later], 63), later)

    def test_an_unreadable_merge_time_is_kept_not_dropped(self):
        # The floor guard throws out merges that PROVABLY predate the issue.
        # A timestamp it cannot parse proves nothing, and dropping the story
        # on that would lose a real delivery to a formatting change.
        odd = pr(5, body="Closes #63", merged_at="not-a-timestamp")
        self.assertIs(
            self.d.pick([odd], 63, not_before="2026-08-01T00:00:00Z"), odd)


if __name__ == "__main__":
    unittest.main()

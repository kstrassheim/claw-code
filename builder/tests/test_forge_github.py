"""GitHub, and the places where it is not like anywhere else.

WHY THESE AND NOT A ROUND TRIP
------------------------------
Every quirk pinned here was written from a production failure, and each is
invisible from inside the planners now that they only see the interface. That
is the point of the seam — and it is also why the failures have to be pinned
HERE, once, rather than rediscovered the next time somebody looks at a plan
that seems reasonable and is not.

The four that matter most:

  * **Discovery is by AUTHORSHIP first.** GitHub refuses to let a pull
    request's author be added as its reviewer (422), and the bot authors
    everything it opens — so a `review-requested:` search can never return the
    bot's own work. Keying discovery on that alone deadlocks the pipeline in
    total silence.
  * **An empty combined status is not green, and not pending either.** GitHub
    answers `state: pending` with an empty list for a commit nobody posted a
    status on. Believing the top-level state waits forever on every repository
    with no CI; ignoring the statuses entirely approves red pull requests.
  * **`state_reason` is what separates delivered from revoked.** It is the
    field the status model reads back, and the only place the difference
    survives a close.
  * **A change request is linked to an issue by branch OR by keyword**, and a
    bare mention is not a link.

No network: a fake transport stands in for the request, so the URLs, the
parameters and the payloads are all asserted rather than performed.
"""

import unittest

from harness import load  # noqa: F401 - puts builder/ on sys.path

import forge  # noqa: E402


class FakeTransport:
    """Answers requests from a routing table, and records every call.

    Routes are matched as a substring of the URL, longest first, so a test
    names only the distinctive part of an endpoint. A route may be a value to
    return or an exception to raise.
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []          # (method, url, params, json_body, form_body)
        self.raw_flags = []      # whether each call asked for text, not JSON

    def __call__(self, method, url, *, headers, params=None, json_body=None,
                 form_body=None, timeout=None, raw=False):
        self.calls.append((method, url, params or {}, json_body, form_body))
        self.raw_flags.append(raw)
        for key in sorted(self.routes, key=len, reverse=True):
            if key in url:
                answer = self.routes[key]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return None

    def urls(self, method=None):
        return [c[1] for c in self.calls if method is None or c[0] == method]

    def params_for(self, fragment):
        for _method, url, params, _j, _f in self.calls:
            if fragment in url:
                return params
        raise AssertionError(f"nothing asked for {fragment!r}: {self.urls()}")


def gh(routes=None):
    t = FakeTransport(routes)
    return forge.GitHubForge("token", transport=t), t


# ---------------------------------------------------------------------------
# the check reduction
# ---------------------------------------------------------------------------


def check_runs(*specs):
    """A check-runs payload. Each spec is (status, conclusion)."""
    return {"check_runs": [{"status": s, "conclusion": c} for s, c in specs]}


def statuses(*states):
    """A combined-status payload with one classic status per state."""
    return {"state": states[0] if states else "pending",
            "statuses": [{"state": s} for s in states]}


NO_RUNS = {"check_runs": []}
# What GitHub answers for a commit nobody posted a status on: state `pending`
# with an empty list. Reading the top-level state alone makes every such
# commit look like a build in progress, forever.
NO_STATUSES = {"state": "pending", "statuses": [], "total_count": 0}

reduce_checks = forge.GitHubForge.reduce_checks


class CheckReduction(unittest.TestCase):
    """green / failed / pending / none, decided in exactly one place."""

    def test_all_successful_is_green(self):
        self.assertEqual(
            reduce_checks(check_runs(("completed", "success"),
                                     ("completed", "success")), NO_STATUSES),
            forge.GREEN)

    def test_neutral_and_skipped_count_as_passing(self):
        # A skipped job is not a failure, and a path-filtered workflow skips
        # constantly. Treating either as "not green" would mean the reviewer
        # never runs on a repo that uses path filters.
        self.assertEqual(
            reduce_checks(check_runs(("completed", "success"),
                                     ("completed", "neutral"),
                                     ("completed", "skipped")), NO_STATUSES),
            forge.GREEN)

    def test_a_failure_is_not_green(self):
        self.assertEqual(
            reduce_checks(check_runs(("completed", "success"),
                                     ("completed", "failure")), NO_STATUSES),
            forge.FAILED)

    def test_timed_out_and_cancelled_are_failures(self):
        for conclusion in ("timed_out", "cancelled", "canceled"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(
                    reduce_checks(check_runs(("completed", conclusion)),
                                  NO_STATUSES),
                    forge.FAILED)

    def test_action_required_is_a_failure_not_a_wait(self):
        # It will never turn green on its own, so waiting for it is waiting
        # forever.
        self.assertEqual(
            reduce_checks(check_runs(("completed", "action_required")),
                          NO_STATUSES),
            forge.FAILED)

    def test_queued_and_in_progress_are_pending(self):
        for status in ("queued", "in_progress"):
            with self.subTest(status=status):
                self.assertEqual(
                    reduce_checks(check_runs((status, None)), NO_STATUSES),
                    forge.PENDING)

    def test_a_failure_beats_a_pending(self):
        # Waiting for the rest of a red build changes nothing.
        self.assertEqual(
            reduce_checks(check_runs(("queued", None),
                                     ("completed", "failure")), NO_STATUSES),
            forge.FAILED)

    def test_a_pending_beats_a_success(self):
        self.assertEqual(
            reduce_checks(check_runs(("completed", "success"),
                                     ("in_progress", None)), NO_STATUSES),
            forge.PENDING)

    def test_an_unrecognised_conclusion_waits_rather_than_passes(self):
        # "I don't know what that means" must never read as green.
        self.assertEqual(
            reduce_checks(check_runs(("completed", "brand_new_thing")),
                          NO_STATUSES),
            forge.PENDING)

    def test_a_stale_run_waits_for_the_fresh_one(self):
        self.assertEqual(
            reduce_checks(check_runs(("completed", "stale")), NO_STATUSES),
            forge.PENDING)

    def test_classic_statuses_are_read_too(self):
        # A repo using only commit statuses has no check-runs at all. Reading
        # only the Checks API would call that green.
        self.assertEqual(reduce_checks(NO_RUNS, statuses("success")),
                         forge.GREEN)
        self.assertEqual(reduce_checks(NO_RUNS, statuses("failure")),
                         forge.FAILED)
        self.assertEqual(reduce_checks(NO_RUNS, statuses("error")),
                         forge.FAILED)
        self.assertEqual(reduce_checks(NO_RUNS, statuses("pending")),
                         forge.PENDING)

    def test_a_red_classic_status_sinks_green_check_runs(self):
        self.assertEqual(
            reduce_checks(check_runs(("completed", "success")),
                          statuses("failure")),
            forge.FAILED)

    def test_no_ci_at_all_is_its_own_answer(self):
        # NOT pending: an empty combined status reports state=pending, and a
        # planner that believed it would wait forever on every repo with no
        # CI configured.
        self.assertEqual(reduce_checks(NO_RUNS, NO_STATUSES), forge.NONE)

    def test_an_empty_combined_status_never_reads_as_green(self):
        # The other half of the same trap: `none` must not be reachable by
        # anything that has actually passed, and green must not be reachable
        # by anything that has not.
        self.assertNotEqual(reduce_checks(NO_RUNS, NO_STATUSES), forge.GREEN)
        self.assertEqual(reduce_checks({}, {"state": "pending",
                                            "statuses": []}), forge.NONE)

    def test_missing_payloads_are_treated_as_no_ci(self):
        self.assertEqual(reduce_checks(None, None), forge.NONE)
        self.assertEqual(reduce_checks({}, {}), forge.NONE)


class ChecksStateFetch(unittest.TestCase):
    """checks_state pairs the two endpoints — and fails towards waiting."""

    def test_it_asks_both_endpoints(self):
        f, t = gh({"check-runs": check_runs(("completed", "success")),
                   "/status": NO_STATUSES})
        self.assertEqual(f.checks_state("o/r", "abc123"), forge.GREEN)
        self.assertTrue(any(u.endswith("/commits/abc123/check-runs")
                            for u in t.urls()), t.urls())
        self.assertTrue(any(u.endswith("/commits/abc123/status")
                            for u in t.urls()), t.urls())

    def test_a_failed_lookup_is_pending_never_green(self):
        # An unknown CI state must not be able to trigger a review.
        f, _ = gh({"check-runs": forge.ForgeError("GitHub 500"),
                   "/status": NO_STATUSES})
        self.assertEqual(f.checks_state("o/r", "abc123"), forge.PENDING)
        f, _ = gh({"check-runs": check_runs(("completed", "success")),
                   "/status": forge.ForgeError("GitHub 500")})
        self.assertEqual(f.checks_state("o/r", "abc123"), forge.PENDING)

    def test_no_sha_is_pending_and_costs_no_call(self):
        f, t = gh()
        self.assertEqual(f.checks_state("o/r", ""), forge.PENDING)
        self.assertEqual(t.calls, [])


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


class ReviewDiscovery(unittest.TestCase):
    """The search queries, and the repository they name."""

    def _searching(self, items):
        f, t = gh({"/user": {"login": "cameron-claw"},
                   "/search/issues": {"items": items}})
        return f, t

    def _queries(self, t):
        return [p.get("q") for m, url, p, _j, _f in t.calls
                if "/search/issues" in url]

    def test_it_searches_by_authorship_first(self):
        # Authorship is the PRIMARY signal, and it has to be, because GitHub
        # refuses to let a pull request's author be added as its reviewer
        # (422). The bot authors everything it opens, so a `review-requested:`
        # search can never return its own work — keying discovery on that
        # alone leaves the solver waiting for a verdict that cannot arrive.
        f, t = self._searching([
            {"number": 1,
             "repository_url": "https://api.github.com/repos/o/r"}])
        items = f.reviewable_change_requests(8)
        self.assertEqual(self._queries(t)[0],
                         "is:pr is:open author:cameron-claw")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["repo"], "o/r")

    def test_it_also_searches_what_it_was_asked_to_review(self):
        # Kept because it is the only way to catch a pull request a HUMAN
        # asked the bot to look at, which authorship would miss.
        f, t = self._searching([])
        f.reviewable_change_requests(8)
        self.assertIn("is:pr is:open review-requested:cameron-claw",
                      self._queries(t))

    def test_a_change_request_found_twice_is_reviewed_once(self):
        f, _ = self._searching([
            {"number": 7,
             "repository_url": "https://api.github.com/repos/o/r"}])
        items = f.reviewable_change_requests(8)
        self.assertEqual(len(items), 1,
                         "authored AND requested must not double up")

    def test_no_login_means_no_search_at_all(self):
        f, t = gh({"/user": {}, "/search/issues": {"items": []}})
        self.assertEqual(f.reviewable_change_requests(8), [])
        self.assertEqual([u for u in t.urls() if "/search/" in u], [])

    def test_every_record_is_stamped_with_the_host_that_found_it(self):
        # Which forge answers a later question about this change request is
        # decided from this stamp, per item — never from a deployment switch.
        f, _ = self._searching([
            {"number": 3,
             "repository_url": "https://api.github.com/repos/o/r"}])
        self.assertEqual(f.reviewable_change_requests(8)[0]["forge"],
                         forge.GITHUB)

    def test_the_repo_comes_from_the_api_url(self):
        # Search items name the repository only by its API URL.
        self.assertEqual(
            forge._repo_from_api_url("https://api.github.com/repos/o/r"),
            "o/r")

    def test_a_malformed_item_names_no_repo(self):
        self.assertEqual(forge._repo_from_api_url(""), "")
        self.assertEqual(forge._repo_from_api_url("nonsense"), "")


class AssignedIssues(unittest.TestCase):
    """One cross-repository listing, minus what is not an issue."""

    def test_pull_requests_are_not_issues(self):
        # They come back from the same endpoint. Planning one as an issue
        # spawns a solver on the bot's own branch.
        f, _ = gh({"/issues": [
            {"number": 1, "title": "a task",
             "repository_url": "https://api.github.com/repos/o/r"},
            {"number": 2, "title": "a change", "pull_request": {},
             "repository_url": "https://api.github.com/repos/o/r"},
        ]})
        by_repo = f.assigned_open_issues()
        self.assertEqual([i["number"] for i in by_repo["o/r"]], [1])

    def test_the_record_is_the_neutral_shape(self):
        f, _ = gh({"/issues": [{
            "number": 4, "title": "t", "body": "b",
            "html_url": "https://github.com/o/r/issues/4",
            "labels": [{"name": "On Hold"}, {"name": "Priority::High"}],
            "state": "open",
            "repository_url": "https://api.github.com/repos/o/r",
        }]})
        got = f.assigned_open_issues()["o/r"][0]
        self.assertEqual(got, {
            "forge": forge.GITHUB, "repo": "o/r", "number": 4,
            "title": "t", "body": "b",
            "url": "https://github.com/o/r/issues/4",
            "labels": ["On Hold", "Priority::High"],
            "state": "open", "closedAs": None,
            # Who filed it — the account the finished work goes back to, and
            # not always the repo owner: the bot files issues itself.
            "author": "",
            # This host serves change requests from the issues collection too,
            # and they arrive looking like issues apart from one key. The
            # distinction is answered here rather than left to a caller.
            "isChangeRequest": False,
            # When it was filed: the delivery sweep tells a change that closed
            # this issue from one that landed before the issue existed.
            "createdAt": "",
        })

    def test_a_short_page_ends_the_listing(self):
        f, t = gh({"/issues": []})
        f.assigned_open_issues()
        self.assertEqual(len([u for u in t.urls() if u.endswith("/issues")]), 1)


class TerminalState(unittest.TestCase):
    """delivered versus revoked, written and read back."""

    def test_delivered_is_recorded_as_completed(self):
        f, t = gh()
        self.assertTrue(f.close_issue("o/r", 5, delivered=True))
        method, url, _p, payload, _f = t.calls[-1]
        self.assertEqual(method, "PATCH")
        self.assertTrue(url.endswith("/repos/o/r/issues/5"), url)
        self.assertEqual(payload, {"state": "closed",
                                   "state_reason": "completed"})

    def test_revoked_is_recorded_as_not_planned(self):
        # The distinction the status model reads back to tell `Done` from
        # `Won't do`. A single close with no reason loses it.
        f, t = gh()
        f.close_issue("o/r", 5, delivered=False)
        self.assertEqual(t.calls[-1][3],
                         {"state": "closed", "state_reason": "not_planned"})

    def test_the_intent_is_read_back_off_a_closed_issue(self):
        f, _ = gh({"/repos/o/r/issues/5": {
            "number": 5, "state": "closed", "state_reason": "not_planned",
            "labels": [{"name": "status::duplicate"}]}})
        self.assertEqual(f.issue("o/r", 5)["closedAs"], forge.REVOKED)

    def test_completed_reads_back_as_delivered(self):
        f, _ = gh({"/repos/o/r/issues/5": {
            "number": 5, "state": "closed", "state_reason": "completed"}})
        self.assertEqual(f.issue("o/r", 5)["closedAs"], forge.DELIVERED)

    def test_a_close_from_before_the_field_existed_is_a_delivery(self):
        # Issues closed before close reasons were a thing carry none, and
        # every one of those shipped.
        f, _ = gh({"/repos/o/r/issues/5": {"number": 5, "state": "closed"}})
        self.assertEqual(f.issue("o/r", 5)["closedAs"], forge.DELIVERED)

    def test_an_open_issue_has_no_terminal_state(self):
        f, _ = gh({"/repos/o/r/issues/5": {"number": 5, "state": "open"}})
        self.assertIsNone(f.issue("o/r", 5)["closedAs"])


class LinkingChangeRequestsToTheIssue(unittest.TestCase):
    """Which open change requests would close this issue."""

    def crs(self, rows):
        f, _ = gh({"/repos/o/r/pulls": rows})
        return f.open_change_requests_for_issue("o/r", 5)

    def test_the_runners_branch_naming_links_it(self):
        self.assertEqual(
            self.crs([{"number": 7, "head": {"ref": "issue-5-fix"},
                       "body": ""}]), [7])

    def test_a_closing_keyword_links_it(self):
        self.assertEqual(
            self.crs([{"number": 8, "head": {"ref": "whatever"},
                       "body": "closes #5"}]), [8])

    def test_a_mere_mention_does_not(self):
        self.assertEqual(
            self.crs([{"number": 9, "head": {"ref": "x"},
                       "body": "unlike #5, this one"}]), [])

    def test_another_issues_branch_does_not(self):
        self.assertEqual(
            self.crs([{"number": 10, "head": {"ref": "issue-51-fix"},
                       "body": ""}]), [])


class Notes(unittest.TestCase):
    """Comments, in the one shape the guard already speaks."""

    def test_the_author_is_named_the_same_way_everywhere(self):
        # lexical_guard reads `author.username`, and a second shape would mean
        # two definitions of "has the bot already asked this?".
        f, _ = gh({"/issues/5/comments": [
            {"id": 1, "body": "hi", "user": {"login": "owner"},
             "created_at": "2026-08-01T10:00:00Z"}]})
        self.assertEqual(f.comments("o/r", 5),
                         [{"id": 1, "body": "hi",
                           "author": {"username": "owner"},
                           # When it was said travels too: "has a person
                           # replied SINCE the bot asked?" is a question about
                           # time, and without it the answer degrades to "the
                           # newest comment", which reads an old reply as new.
                           "createdAt": "2026-08-01T10:00:00Z"}])

    def test_a_payload_that_is_not_a_list_is_an_error_not_an_empty_thread(self):
        # An empty thread means "nobody has said anything", which is a real
        # answer several gates act on. An unreadable one is not.
        f, _ = gh({"/issues/5/comments": {"message": "boom"}})
        with self.assertRaises(forge.ForgeError):
            f.comments("o/r", 5)


class ChangeRequests(unittest.TestCase):
    """The record the review gate turns on."""

    def test_the_head_and_the_draft_flag_travel_together(self):
        f, _ = gh({"/pulls/7": {
            "number": 7, "state": "open", "draft": True, "title": "t",
            "head": {"sha": "abc", "ref": "issue-5-fix"},
            "base": {"ref": "main"}}})
        cr = f.change_request("o/r", 7)
        self.assertEqual((cr["headSha"], cr["headRef"], cr["baseRef"],
                          cr["draft"], cr["state"]),
                         ("abc", "issue-5-fix", "main", True, "open"))

    def test_a_merged_change_request_says_merged(self):
        f, _ = gh({"/pulls/7": {"number": 7, "state": "closed",
                                "merged_at": "2026-01-01T00:00:00Z"}})
        self.assertEqual(f.change_request("o/r", 7)["state"], "merged")

    def test_an_unreadable_change_request_is_empty_not_a_crash(self):
        # The planner reports `pr-fetch-failed` and moves on to the next one.
        f, _ = gh({"/pulls/7": forge.ForgeError("GitHub 500")})
        self.assertEqual(f.change_request("o/r", 7), {})

    def test_verdicts_carry_the_author_and_the_commit(self):
        f, _ = gh({"/pulls/7/reviews": [
            {"user": {"login": "owner"}, "state": "CHANGES_REQUESTED",
             "body": "no", "commit_id": "abc"}]})
        self.assertEqual(f.review_verdicts("o/r", 7), [
            {"author": "owner", "verdict": "changes_requested",
             "body": "no", "sha": "abc"}])


class Vocabulary(unittest.TestCase):
    def test_the_user_facing_noun_is_a_pull_request(self):
        f, _ = gh()
        self.assertEqual(f.change_request_noun, "pull request")
        self.assertEqual(f.kind, forge.GITHUB)


if __name__ == "__main__":
    unittest.main()

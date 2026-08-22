"""Every question in the interface, asked of every host that answers it.

WHY THIS FILE EXISTS
--------------------
The per-host files pin the places the hosts DIFFER, which is the interesting
half and was written first. This is the boring half, and it is the half that
was missing: of the interface's 36 methods, ten were exercised for one host
and twelve for another. A method with no test is a method whose URL nobody has
ever read back — and a wrong URL does not raise, it 404s, and every caller in
this bot reads a failed read as "there is nothing there".

That gap stopped being theoretical when a third host arrived that cannot be
tried against a real server before it ships. So every method is called here
for every implementation, and for Gitea the exact request is asserted against
the endpoints in its published OpenAPI description — path, verb and body —
because for that host the test is the only thing standing between a typo and
a silent empty answer in production.

Two kinds of assertion, and the second is the one that matters:

  * the ANSWER is in this repository's neutral vocabulary;
  * the REQUEST went where the host's API actually is.

No network: a fake transport stands in for the request.
"""

import unittest

from harness import load  # noqa: F401 - puts builder/ on sys.path

import forge  # noqa: E402
import forge_azdo  # noqa: E402 - _short_ref is pinned directly
from test_forge_github import FakeTransport  # noqa: E402

GITEA_BASE = "https://gitea.example.invalid"
GITLAB_BASE = "https://gitlab.example.invalid"
REPO = "acme/app"


def gitea(routes=None):
    t = FakeTransport(routes)
    return forge.GiteaForge(GITEA_BASE, "token", transport=t), t


def github(routes=None):
    t = FakeTransport(routes)
    return forge.GitHubForge("token", transport=t), t


def gitlab(routes=None):
    t = FakeTransport(routes)
    return forge.GitLabForge(GITLAB_BASE, "token", transport=t), t


AZDO_BASE = "https://dev.azure.com/acme"


def azdo(routes=None):
    t = FakeTransport(routes)
    f = forge.AzureDevOpsForge(AZDO_BASE, "token", transport=t)
    # Every Git call is addressed by repository GUID, so the index is primed
    # rather than left to a live lookup in each test.
    f._repos = {"proj/app": "11111111-1111-1111-1111-111111111111",
                "11111111-1111-1111-1111-111111111111": "proj/app"}
    return f, t


ALL = (("gitea", gitea), ("github", github), ("gitlab", gitlab),
       ("azdo", azdo))


class EveryHostAnswersEveryQuestion(unittest.TestCase):
    """No method may be missing, and none may answer by raising.

    The exceptions are declared rather than discovered: a host that genuinely
    cannot answer says so with NotImplementedError naming the gap, which is a
    different thing from a method nobody implemented.
    """

    def test_every_abstract_method_is_implemented_by_every_host(self):
        for name in sorted(forge.Forge.__abstractmethods__):
            for label, build in ALL:
                impl, _ = build()
                with self.subTest(method=name, host=label):
                    self.assertIsNot(
                        getattr(type(impl), name), getattr(forge.Forge, name),
                        f"{label} inherits {name} from the ABC")

    def test_azure_devops_says_which_questions_it_cannot_answer(self):
        impl, _ = azdo()
        for call, needle in ((lambda: impl.security_findings("proj/app", 1),
                              "code-scanning"),
                             (lambda: impl.comment_on_commit("proj/app", "s", "x"),
                              "commit-comment"),
                             (lambda: impl.react("proj/app", 1, 2, "+1"),
                              "reaction")):
            with self.assertRaises(NotImplementedError) as caught:
                call()
            self.assertIn(needle, str(caught.exception))

    def test_a_host_that_cannot_answer_says_which_question(self):
        impl, _ = gitea()
        for call, needle in ((lambda: impl.security_findings(REPO, 1),
                              "code-scanning"),
                             (lambda: impl.comment_on_commit(REPO, "sha", "x"),
                              "commit-comment")):
            with self.assertRaises(NotImplementedError) as caught:
                call()
            self.assertIn(needle, str(caught.exception))


class GiteaAsksTheDocumentedEndpoint(unittest.TestCase):
    """Path, verb and body, against Gitea's published API description.

    Every expectation here was read out of Gitea's OpenAPI description rather
    than remembered. The three that are NOT the obvious guess are called out
    in their own tests below, because each is a silent failure rather than a
    loud one.
    """

    def assert_called(self, transport, method, fragment):
        for verb, url, _p, _j, _f in transport.calls:
            if verb == method and fragment in url:
                return
        self.fail(f"no {method} to ...{fragment}; saw {transport.calls}")

    def test_the_token_goes_in_the_header_not_the_query(self):
        # The `access_token` query parameter also authenticates and is
        # deprecated for removal — and a token in a URL lands in access logs.
        impl, t = gitea({"/user": {"login": "bot"}})
        impl.bot_identity()
        headers = forge.GiteaForge(GITEA_BASE, "secret")._headers()
        self.assertEqual(headers["Authorization"], "token secret")
        self.assertNotIn("access_token", t.urls()[0])

    def test_the_api_is_rooted_at_api_v1(self):
        impl, t = gitea({"/user": {"login": "bot"}})
        impl.bot_identity()
        self.assertTrue(t.urls()[0].startswith(f"{GITEA_BASE}/api/v1"))

    def test_reads(self):
        cases = [
            ("bot_identity", (), "/api/v1/user"),
            ("accessible_repos", (5,), "/user/repos"),
            ("assigned_open_issues", (), "/repos/issues/search"),
            ("reviewable_change_requests", (5,), "/repos/issues/search"),
            ("issue", (REPO, 7), "/repos/acme/app/issues/7"),
            ("comments", (REPO, 7), "/repos/acme/app/issues/7/comments"),
            ("change_request", (REPO, 7), "/repos/acme/app/pulls/7"),
            ("change_request_files", (REPO, 7), "/repos/acme/app/pulls/7/files"),
            ("review_verdicts", (REPO, 7), "/repos/acme/app/pulls/7/reviews"),
            ("review_requests", (REPO, 7), "/repos/acme/app/pulls/7"),
            ("checks", (REPO, "abc"), "/repos/acme/app/commits/abc/statuses"),
            ("open_issues", (REPO,), "/repos/acme/app/issues"),
            ("recent_change_requests", (REPO,), "/repos/acme/app/pulls"),
            ("branch_head", (REPO, "main"), "/repos/acme/app/branches/main"),
            ("file_at_ref", (REPO, "a.txt", "main"),
             "/repos/acme/app/contents/a.txt"),
        ]
        for name, args, fragment in cases:
            impl, t = gitea()
            with self.subTest(method=name):
                getattr(impl, name)(*args)
                self.assert_called(t, "GET", fragment)

    def test_writes(self):
        cases = [
            ("post_comment", (REPO, 7, "hi"), "POST",
             "/repos/acme/app/issues/7/comments"),
            ("post_change_request_comment", (REPO, 7, "hi"), "POST",
             "/repos/acme/app/issues/7/comments"),
            ("add_labels", (REPO, 7, ["a"]), "POST",
             "/repos/acme/app/issues/7/labels"),
            ("close_change_request", (REPO, 7), "PATCH",
             "/repos/acme/app/pulls/7"),
            ("merge", (REPO, 7), "POST", "/repos/acme/app/pulls/7/merge"),
            ("submit_review", (REPO, 7, "approve", "ok"), "POST",
             "/repos/acme/app/pulls/7/reviews"),
            ("request_review", (REPO, 7, ["u"]), "POST",
             "/repos/acme/app/pulls/7/requested_reviewers"),
            ("remove_review_request", (REPO, 7, ["u"]), "DELETE",
             "/repos/acme/app/pulls/7/requested_reviewers"),
            ("react", (REPO, 7, 42, "+1"), "POST",
             "/repos/acme/app/issues/comments/42/reactions"),
            ("create_issue", (REPO, "t"), "POST", "/repos/acme/app/issues"),
        ]
        for name, args, verb, fragment in cases:
            impl, t = gitea()
            with self.subTest(method=name):
                getattr(impl, name)(*args)
                self.assert_called(t, verb, fragment)

    def test_merge_names_the_strategy_in_the_field_the_host_reads(self):
        # `do`, and one of a closed set of values. A body this host does not
        # recognise is not a merge that happens differently — it is a 422.
        impl, t = gitea()
        impl.merge(REPO, 7, squash=True)
        self.assertEqual(t.calls[-1][3]["do"], "squash")
        impl, t = gitea()
        impl.merge(REPO, 7, squash=False)
        self.assertEqual(t.calls[-1][3]["do"], "merge")

    def test_a_review_verdict_uses_the_hosts_event_vocabulary(self):
        for verdict, event in (("approve", "APPROVED"),
                               ("request-changes", "REQUEST_CHANGES"),
                               ("comment", "COMMENT")):
            impl, t = gitea()
            impl.submit_review(REPO, 7, verdict, "body")
            with self.subTest(verdict=verdict):
                self.assertEqual(t.calls[-1][3]["event"], event)


class TheThreeGiteaTrapsAreHandled(unittest.TestCase):
    """The places Gitea looks like GitHub and is not.

    Each of these fails QUIETLY if it is got wrong — a 404 or a 422 that every
    caller reads as "there is nothing there" — which is why each has a test of
    its own rather than being folded into the table above.
    """

    def test_a_label_is_removed_by_id_because_a_name_addresses_nothing(self):
        # The delete endpoint's path parameter is an integer. A name posted
        # there 404s, and a caller reads that as "the issue is gone".
        impl, t = gitea({"/labels": [{"id": 31, "name": "status::parked"}]})
        self.assertTrue(impl.remove_label(REPO, 7, "status::parked"))
        self.assertIn(f"{GITEA_BASE}/api/v1/repos/acme/app/issues/7/labels/31",
                      t.urls("DELETE"))

    def test_removing_a_label_the_repository_never_defined_is_not_a_failure(self):
        impl, t = gitea({"/labels": []})
        self.assertTrue(impl.remove_label(REPO, 7, "nope"))
        self.assertEqual(t.urls("DELETE"), [])

    def test_creating_an_issue_sends_label_ids_not_names(self):
        # Adding a label to an EXISTING issue accepts names; creating one
        # accepts ids only. The same word, two meanings, one call apart.
        impl, t = gitea({"/labels": [{"id": 9, "name": "bug"}],
                         "/issues": {"number": 12}})
        self.assertEqual(impl.create_issue(REPO, "t", labels=["bug"]), 12)
        posted = [c for c in t.calls if c[0] == "POST"][-1]
        self.assertEqual(posted[3]["labels"], [9])

    def test_adding_a_label_to_an_existing_issue_sends_names(self):
        impl, t = gitea()
        impl.add_labels(REPO, 7, ["bug"])
        self.assertEqual(t.calls[-1][3]["labels"], ["bug"])


class TheNeutralShapeIsTheSameFromEveryHost(unittest.TestCase):
    """A record, once translated, does not say where it came from.

    `test_forge_shapes` pins this for the two hosts it was written for, from a
    shared starting fact. This adds the third to the same rule.
    """

    def test_an_issue_reads_the_same(self):
        rows = {
            "gitea": (gitea, {"/issues/7": {
                "number": 7, "title": "T", "body": "B", "state": "open",
                "html_url": "u", "labels": [{"name": "bug"}],
                "user": {"login": "ann"}, "created_at": "t0"}}),
            "github": (github, {"/issues/7": {
                "number": 7, "title": "T", "body": "B", "state": "open",
                "html_url": "u", "labels": [{"name": "bug"}],
                "user": {"login": "ann"}, "created_at": "t0"}}),
            "gitlab": (gitlab, {"/issues/7": {
                "iid": 7, "title": "T", "description": "B", "state": "opened",
                "web_url": "u", "labels": ["bug"],
                "author": {"username": "ann"}, "created_at": "t0"}}),
        }
        for label, (build, routes) in rows.items():
            impl, _ = build(routes)
            got = impl.issue(REPO, 7)
            with self.subTest(host=label):
                self.assertEqual(got["number"], 7)
                self.assertEqual(got["title"], "T")
                self.assertEqual(got["body"], "B")
                self.assertEqual(got["labels"], ["bug"])
                self.assertEqual(got["state"], "open")
                self.assertEqual(got["author"], "ann")
                self.assertEqual(got["forge"], label)

    def test_a_closed_issue_records_whether_the_work_shipped(self):
        # Gitea has no native close reason, so it reads the status label back
        # — the same convention GitLab uses, and the reason the interface
        # takes an intent rather than a field.
        impl, _ = gitea({"/issues/7": {
            "number": 7, "state": "closed",
            "labels": [{"name": "status::wont-do"}]}})
        self.assertEqual(impl.issue(REPO, 7)["closedAs"], forge.REVOKED)
        impl, _ = gitea({"/issues/7": {"number": 7, "state": "closed",
                                       "labels": []}})
        self.assertEqual(impl.issue(REPO, 7)["closedAs"], forge.DELIVERED)

    def test_a_merged_change_request_does_not_read_as_abandoned(self):
        # `state` stays "closed" on a landed pull request here; only `merged`
        # tells them apart.
        impl, _ = gitea({"/pulls/7": {"number": 7, "state": "closed",
                                      "merged": True,
                                      "head": {"sha": "s", "ref": "h"},
                                      "base": {"ref": "main"}}})
        self.assertEqual(impl.change_request(REPO, 7)["state"], "merged")
        impl, _ = gitea({"/pulls/7": {"number": 7, "state": "closed",
                                      "head": {"sha": "s", "ref": "h"},
                                      "base": {"ref": "main"}}})
        self.assertEqual(impl.change_request(REPO, 7)["state"], "closed")


class TheCheckReductionAgreesAcrossHosts(unittest.TestCase):
    """`checks_state` and `checks` come from one reduction, on every host."""

    def test_no_ci_at_all_is_none_not_pending(self):
        for label, build in ALL:
            impl, _ = build({"status": [], "pipelines": [],
                             "check-runs": {"check_runs": []},
                             "commits": []})
            with self.subTest(host=label):
                self.assertEqual(impl.checks_state(REPO, "abc"), forge.NONE)

    def test_gitea_reduces_each_status(self):
        for state, expected in (("success", forge.GREEN),
                                ("failure", forge.FAILED),
                                ("error", forge.FAILED),
                                ("pending", forge.PENDING),
                                ("something-new", forge.PENDING)):
            impl, _ = gitea({"/statuses": [
                {"context": "ci", "status": state, "created_at": "t1"}]})
            with self.subTest(state=state):
                self.assertEqual(impl.checks_state(REPO, "abc"), expected)
                self.assertEqual(impl.checks(REPO, "abc"),
                                 [{"name": "ci", "state": expected}])

    def test_gitea_counts_only_the_newest_status_per_context(self):
        # This host appends rather than replaces, so a context that failed and
        # was re-run keeps both rows. Counting the stale one gates a green
        # commit as red forever.
        impl, _ = gitea({"/statuses": [
            {"context": "ci", "status": "failure", "created_at": "t1"},
            {"context": "ci", "status": "success", "created_at": "t2"}]})
        self.assertEqual(impl.checks_state(REPO, "abc"), forge.GREEN)

    def test_a_failure_anywhere_outranks_a_pending(self):
        impl, _ = gitea({"/statuses": [
            {"context": "a", "status": "pending", "created_at": "t1"},
            {"context": "b", "status": "failure", "created_at": "t1"}]})
        self.assertEqual(impl.checks_state(REPO, "abc"), forge.FAILED)


class DeletingABranchRefusesTheDefault(unittest.TestCase):
    """The one irreversible call in the interface, on every host."""

    def test_gitea_refuses_the_default_branch(self):
        impl, t = gitea({"/repos/acme/app": {"default_branch": "main"}})
        self.assertFalse(impl.delete_branch(REPO, "main"))
        self.assertEqual(t.urls("DELETE"), [])

    def test_gitea_deletes_any_other_branch(self):
        impl, t = gitea({"/repos/acme/app": {"default_branch": "main"}})
        self.assertTrue(impl.delete_branch(REPO, "topic"))
        self.assertIn(f"{GITEA_BASE}/api/v1/repos/acme/app/branches/topic",
                      t.urls("DELETE"))


class WritesReportRatherThanRaise(unittest.TestCase):
    """A planner that cannot write must still plan the rest of the tick."""

    def test_every_write_returns_false_when_the_host_refuses(self):
        boom = forge.ForgeError("nope", code=500)
        cases = [("post_comment", (REPO, 7, "x")),
                 ("post_change_request_comment", (REPO, 7, "x")),
                 ("add_labels", (REPO, 7, ["a"])),
                 ("close_issue", (REPO, 7, True)),
                 ("close_change_request", (REPO, 7)),
                 ("merge", (REPO, 7)),
                 ("submit_review", (REPO, 7, "approve", "b")),
                 ("request_review", (REPO, 7, ["u"])),
                 ("remove_review_request", (REPO, 7, ["u"])),
                 ("react", (REPO, 7, 1, "+1"))]
        for name, args in cases:
            impl, _ = gitea({"": boom})
            with self.subTest(method=name):
                self.assertIs(getattr(impl, name)(*args), False)

    def test_unreadable_reads_answer_empty_rather_than_raise(self):
        boom = forge.ForgeError("nope", code=500)
        impl, _ = gitea({"": boom})
        self.assertEqual(impl.issue(REPO, 7), {})
        self.assertEqual(impl.change_request(REPO, 7), {})
        self.assertEqual(impl.comments(REPO, 7), [])
        self.assertEqual(impl.open_issues(REPO), [])
        self.assertEqual(impl.recent_change_requests(REPO), [])
        self.assertEqual(impl.change_request_files(REPO, 7), [])
        self.assertEqual(impl.review_verdicts(REPO, 7), [])
        self.assertEqual(impl.review_requests(REPO, 7), [])
        self.assertEqual(impl.branch_head(REPO, "main"), "")
        self.assertEqual(impl.file_at_ref(REPO, "a", "main"), "")
        self.assertEqual(impl.default_branch_head(REPO), ("", ""))
        self.assertEqual(impl.failing_check_log(REPO, "abc"), "")
        self.assertEqual(impl.create_issue(REPO, "t"), 0)


class TheFailingLogIsWalkedFromRunToJob(unittest.TestCase):
    """This host keeps CI results and CI logs in different places."""

    def test_gitea_walks_run_then_job_then_log(self):
        impl, t = gitea({
            "/actions/runs?": None,
            "/actions/runs": {"workflow_runs": [
                {"id": 5, "conclusion": "failure"}]},
            "/actions/runs/5/jobs": {"jobs": [
                {"id": 9, "conclusion": "failure"}]},
            "/actions/jobs/9/logs": "line one\nline two",
        })
        self.assertEqual(impl.failing_check_log(REPO, "abc"),
                         "line one\nline two")
        self.assertEqual(t.params_for("/actions/runs")["head_sha"], "abc")
        self.assertTrue(any(t.raw_flags), "the log must be read as text")

    def test_a_log_is_truncated_from_the_end(self):
        impl, _ = gitea({
            "/actions/runs": {"workflow_runs": [
                {"id": 5, "conclusion": "failure"}]},
            "/actions/runs/5/jobs": {"jobs": [
                {"id": 9, "conclusion": "failure"}]},
            "/actions/jobs/9/logs": "abcdefghij",
        })
        # A failure says what went wrong on its LAST lines.
        self.assertEqual(impl.failing_check_log(REPO, "abc", limit=4), "ghij")


class ALabelIsDefinedBeforeItIsUsed(unittest.TestCase):
    def test_already_existing_is_success(self):
        impl, t = gitea({"/labels": [{"id": 3, "name": "bug"}]})
        self.assertTrue(impl.ensure_label(REPO, "bug"))
        self.assertEqual(t.urls("POST"), [])

    def test_a_new_label_is_created(self):
        impl, t = gitea({"/labels": []})
        self.assertTrue(impl.ensure_label(REPO, "bug", "ff0000", "d"))
        self.assertIn(f"{GITEA_BASE}/api/v1/repos/acme/app/labels",
                      t.urls("POST"))
        self.assertEqual(t.calls[-1][3]["color"], "ff0000")

    def test_a_hash_is_stripped_from_the_colour(self):
        impl, t = gitea({"/labels": []})
        impl.ensure_label(REPO, "bug", "#00ff00")
        self.assertEqual(t.calls[-1][3]["color"], "00ff00")


class ClosingAnIssueRecordsTheIntent(unittest.TestCase):
    def test_gitea_writes_the_status_label_when_the_work_was_called_off(self):
        impl, t = gitea({"/labels": []})
        self.assertTrue(impl.close_issue(REPO, 7, delivered=False))
        bodies = [c[3] for c in t.calls if c[0] == "POST"]
        self.assertIn({"labels": ["status::wont-do"]}, bodies)
        patched = [c for c in t.calls if c[0] == "PATCH"][-1]
        self.assertEqual(patched[3], {"state": "closed"})

    def test_gitea_clears_the_status_label_when_the_work_shipped(self):
        impl, t = gitea({"/labels": [{"id": 4, "name": "status::wont-do"}]})
        self.assertTrue(impl.close_issue(REPO, 7, delivered=True))
        self.assertTrue(any("/labels/4" in u for u in t.urls("DELETE")))


class FileContentIsDecoded(unittest.TestCase):
    def test_gitea_decodes_base64_content(self):
        impl, _ = gitea({"/contents/": {"encoding": "base64",
                                        "content": "aGVsbG8="}})
        self.assertEqual(impl.file_at_ref(REPO, "a.txt", "main"), "hello")

    def test_a_missing_file_is_empty_rather_than_an_error(self):
        impl, _ = gitea({"/contents/": forge.ForgeError("404", code=404)})
        self.assertEqual(impl.file_at_ref(REPO, "a.txt", "main"), "")


class TheGiteaHelpersThatAddressingDependsOn(unittest.TestCase):
    """The small functions every other Gitea call is built out of.

    Each is exercised through a public method elsewhere in this file. They are
    also pinned directly, because an addressing bug does not raise — it builds
    a URL for somewhere else, and the host answers 404 to a caller that reads
    404 as "there is nothing there".
    """

    def test_split_separates_owner_from_name(self):
        self.assertEqual(forge.GiteaForge._split("acme/app"), ("acme", "app"))
        # A name with no owner is not guessed at.
        self.assertEqual(forge.GiteaForge._split("app"), ("app", ""))
        self.assertEqual(forge.GiteaForge._split(""), ("", ""))

    def test_repo_path_addresses_by_segment_not_by_one_encoded_string(self):
        impl, _ = gitea()
        # Unlike GitLab, where a project is one URL-encoded path.
        self.assertEqual(impl._repo_path("acme/app"), "/repos/acme/app")

    def test_repo_path_escapes_a_segment(self):
        impl, _ = gitea()
        self.assertEqual(impl._repo_path("a b/c d"), "/repos/a%20b/c%20d")

    def test_repo_from_raw_prefers_full_name(self):
        self.assertEqual(
            forge.GiteaForge._repo_from_raw({"repository": {"full_name": "a/b"}}),
            "a/b")

    def test_repo_from_raw_falls_back_to_owner_and_name(self):
        self.assertEqual(
            forge.GiteaForge._repo_from_raw(
                {"repository": {"owner": {"login": "a"}, "name": "b"}}),
            "a/b")

    def test_repo_from_raw_answers_empty_rather_than_a_half_name(self):
        # "a/" addresses a repository that does not exist; "" is skipped.
        self.assertEqual(
            forge.GiteaForge._repo_from_raw({"repository": {"name": "b"}}), "")
        self.assertEqual(forge.GiteaForge._repo_from_raw({}), "")

    def test_reduce_status_maps_every_state_this_host_reports(self):
        r = forge.GiteaForge._reduce_status
        self.assertEqual(r("success"), forge.GREEN)
        self.assertEqual(r("failure"), forge.FAILED)
        self.assertEqual(r("error"), forge.FAILED)
        self.assertEqual(r("pending"), forge.PENDING)
        self.assertEqual(r("running"), forge.PENDING)
        # An unknown state must never read as green.
        self.assertEqual(r("invented-tomorrow"), forge.PENDING)
        self.assertEqual(r(""), forge.PENDING)

    def test_label_id_finds_a_label_case_insensitively(self):
        impl, _ = gitea({"/labels": [{"id": 3, "name": "Status::Parked"}]})
        self.assertEqual(impl._label_id(REPO, "status::parked"), 3)

    def test_label_id_is_zero_when_the_repository_has_no_such_label(self):
        impl, _ = gitea({"/labels": [{"id": 3, "name": "bug"}]})
        self.assertEqual(impl._label_id(REPO, "nope"), 0)

    def test_label_id_is_zero_rather_than_raising_when_labels_cannot_be_read(self):
        impl, _ = gitea({"/labels": forge.ForgeError("boom", code=500)})
        self.assertEqual(impl._label_id(REPO, "bug"), 0)


class AzureDevOpsAsksTheDocumentedEndpoint(unittest.TestCase):
    """Path, verb and body, against the published 7.1 REST specification.

    Every expectation here was read out of the spec rather than remembered.
    The traps below are each a silent failure: a wrong route 404s and a
    missing api-version is rejected outright, and both reach a caller as
    "there is nothing there".
    """

    def assert_called(self, transport, method, fragment):
        for verb, url, _p, _j, _f in transport.calls:
            if verb == method and fragment in url:
                return
        self.fail(f"no {method} to ...{fragment}; saw {transport.urls()}")

    def test_every_request_carries_the_api_version(self):
        # Not a default anyone may rely on — a call without it is rejected,
        # and the message names the version rather than what was asked.
        impl, t = azdo({"/pullrequests/7": {"pullRequestId": 7}})
        impl.change_request("proj/app", 7)
        impl.checks("proj/app", "abc")
        impl.branch_head("proj/app", "main")
        for _verb, _url, params, _j, _f in t.calls:
            self.assertEqual(params.get("api-version"), "7.1")

    def test_the_pat_is_basic_auth_with_an_empty_username(self):
        # A bearer header 401s, which reads exactly like a bad token.
        import base64
        headers = forge.AzureDevOpsForge(AZDO_BASE, "secret")._headers()
        self.assertEqual(headers["Authorization"],
                         "Basic " + base64.b64encode(b":secret").decode())

    def test_git_calls_address_the_repository_by_guid(self):
        # A repository NAME is unique only inside its project; the GUID is
        # the org-unique handle and the only thing these routes accept.
        impl, t = azdo()
        impl.change_request("proj/app", 7)
        self.assert_called(
            t, "GET",
            "/proj/_apis/git/repositories/11111111-1111-1111-1111-111111111111"
            "/pullrequests/7")

    def test_an_unknown_repository_is_refused_rather_than_guessed(self):
        impl, t = azdo()
        self.assertEqual(impl.change_request("proj/missing", 7), {})
        self.assertEqual(t.calls, [])

    def test_reads(self):
        R = "/proj/_apis/git/repositories/11111111-1111-1111-1111-111111111111"
        for name, args, fragment in [
                ("change_request", ("proj/app", 7), f"{R}/pullrequests/7"),
                ("change_request_comments", ("proj/app", 7),
                 f"{R}/pullRequests/7/threads"),
                ("checks", ("proj/app", "abc"), f"{R}/commits/abc/statuses"),
                ("review_verdicts", ("proj/app", 7), f"{R}/pullrequests/7"),
                ("review_requests", ("proj/app", 7), f"{R}/pullrequests/7"),
                ("branch_head", ("proj/app", "main"), f"{R}/refs"),
                ("file_at_ref", ("proj/app", "a.txt", "abc"), f"{R}/items"),
                ("recent_change_requests", ("proj/app",), f"{R}/pullrequests"),
                ("issue", ("proj/app", 7), "/proj/_apis/wit/workitems/7"),
                ("comments", ("proj/app", 7),
                 "/proj/_apis/wit/workItems/7/comments")]:
            impl, t = azdo()
            with self.subTest(method=name):
                getattr(impl, name)(*args)
                self.assert_called(t, "GET", fragment)

    def test_a_work_item_edit_is_a_json_patch(self):
        # This endpoint rejects application/json outright, and the message
        # names the media type rather than the field that was wrong.
        impl, t = azdo()
        impl.post_comment("proj/app", 7, "hello")
        verb, url, _p, body, _f = t.calls[-1]
        self.assertEqual(verb, "PATCH")
        self.assertIn("/proj/_apis/wit/workitems/7", url)
        self.assertEqual(body, [{"op": "add",
                                 "path": "/fields/System.History",
                                 "value": "hello"}])

    def test_completing_a_pull_request_sends_the_head_it_read(self):
        # lastMergeSourceCommit is required, and a stale one is refused
        # rather than merging the wrong tree.
        impl, t = azdo({"/pullrequests/7": {
            "pullRequestId": 7, "status": "active",
            "lastMergeSourceCommit": {"commitId": "deadbeef"}}})
        self.assertTrue(impl.merge("proj/app", 7, squash=True))
        body = t.calls[-1][3]
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["lastMergeSourceCommit"]["commitId"], "deadbeef")
        self.assertEqual(body["completionOptions"]["mergeStrategy"], "squash")

    def test_a_pull_request_with_no_head_is_not_merged(self):
        impl, t = azdo({"/pullrequests/7": {"pullRequestId": 7}})
        self.assertFalse(impl.merge("proj/app", 7))
        self.assertEqual(t.urls("PATCH"), [])

    def test_abandoning_is_not_completing(self):
        impl, t = azdo()
        impl.close_change_request("proj/app", 7)
        self.assertEqual(t.calls[-1][3], {"status": "abandoned"})

    def test_deleting_a_branch_is_an_update_to_an_all_zero_object(self):
        guid = "11111111-1111-1111-1111-111111111111"
        impl, t = azdo({f"/repositories/{guid}": {
                            "defaultBranch": "refs/heads/main"},
                        f"/repositories/{guid}/refs": {
                            "value": [{"name": "refs/heads/topic",
                                       "objectId": "cafe"}]}})
        self.assertTrue(impl.delete_branch("proj/app", "topic"))
        body = t.calls[-1][3]
        self.assertEqual(body[0]["newObjectId"], "0" * 40)
        self.assertEqual(body[0]["oldObjectId"], "cafe")

    def test_the_default_branch_is_refused(self):
        guid = "11111111-1111-1111-1111-111111111111"
        impl, t = azdo({f"/repositories/{guid}": {
                            "defaultBranch": "refs/heads/main"},
                        f"/repositories/{guid}/refs": {
                            "value": [{"name": "refs/heads/main",
                                       "objectId": "cafe"}]}})
        self.assertFalse(impl.delete_branch("proj/app", "main"))
        self.assertEqual(t.urls("POST"), [])


class AzureDevOpsSpeaksItsOwnVocabulary(unittest.TestCase):
    """Work items are not issues, and a review is a number."""

    def test_a_work_item_becomes_a_neutral_issue(self):
        impl, _ = azdo({"/wit/workitems/7": {
            "id": 7,
            "fields": {"System.Title": "T",
                       "System.Description": "B",
                       "System.State": "Active",
                       "System.Tags": "bug; ui",
                       "System.CreatedBy": {"uniqueName": "ann"},
                       "System.CreatedDate": "t0"}}})
        got = impl.issue("proj/app", 7)
        self.assertEqual(got["number"], 7)
        self.assertEqual(got["title"], "T")
        # System.Description is the body the destructive-wording guard reads.
        self.assertEqual(got["body"], "B")
        # Tags are ONE semicolon-joined string, not a list.
        self.assertEqual(got["labels"], ["bug", "ui"])
        self.assertEqual(got["state"], "open")
        self.assertEqual(got["author"], "ann")
        self.assertEqual(got["forge"], "azdo")
        self.assertIs(got["isChangeRequest"], False)

    def test_closed_is_decided_by_category_not_by_the_state_name(self):
        # Agile closes to "Closed", Scrum to "Done", and a customised process
        # can spell it anything. The category is the portable answer.
        for category, state in (("Completed", "Shipped It"),
                                ("Removed", "Nope")):
            impl, _ = azdo({"/wit/workitems/7": {"id": 7, "fields": {
                "System.State": state,
                "System.StateCategory": category}}})
            with self.subTest(category=category):
                self.assertEqual(impl.issue("proj/app", 7)["state"], "closed")

    def test_a_removed_work_item_reads_as_revoked_not_delivered(self):
        impl, _ = azdo({"/wit/workitems/7": {"id": 7, "fields": {
            "System.State": "Removed", "System.StateCategory": "Removed"}}})
        self.assertEqual(impl.issue("proj/app", 7)["closedAs"], forge.REVOKED)
        impl, _ = azdo({"/wit/workitems/7": {"id": 7, "fields": {
            "System.State": "Closed", "System.StateCategory": "Completed"}}})
        self.assertEqual(impl.issue("proj/app", 7)["closedAs"], forge.DELIVERED)

    def test_completed_is_merged_and_abandoned_is_closed(self):
        for status, expected in (("active", "open"),
                                 ("completed", "merged"),
                                 ("abandoned", "closed")):
            impl, _ = azdo({"/pullrequests/7": {
                "pullRequestId": 7, "status": status,
                "sourceRefName": "refs/heads/topic",
                "targetRefName": "refs/heads/main"}})
            with self.subTest(status=status):
                got = impl.change_request("proj/app", 7)
                self.assertEqual(got["state"], expected)
                # Refs are fully qualified here; callers want the short name.
                self.assertEqual(got["headRef"], "topic")
                self.assertEqual(got["baseRef"], "main")

    def test_a_review_is_a_vote(self):
        impl, _ = azdo({"/pullrequests/7": {"pullRequestId": 7, "reviewers": [
            {"uniqueName": "a", "vote": 10},
            {"uniqueName": "b", "vote": 5},
            {"uniqueName": "c", "vote": -10},
            {"uniqueName": "d", "vote": -5},
            {"uniqueName": "e", "vote": 0}]}})
        got = {v["author"]: v["verdict"] for v in
               impl.review_verdicts("proj/app", 7)}
        self.assertEqual(got["a"], "approved")
        self.assertEqual(got["b"], "approved")
        self.assertEqual(got["c"], "changes_requested")
        self.assertEqual(got["d"], "changes_requested")
        # 0 is "has not voted", which is not a verdict.
        self.assertNotIn("e", got)

    def test_the_status_reduction(self):
        for state, expected in (("succeeded", forge.GREEN),
                                ("failed", forge.FAILED),
                                ("error", forge.FAILED),
                                ("pending", forge.PENDING),
                                ("notSet", forge.PENDING),
                                ("invented", forge.PENDING)):
            impl, _ = azdo({"/statuses": {"value": [
                {"state": state, "context": {"genre": "ci", "name": "build"}}]}})
            with self.subTest(state=state):
                self.assertEqual(impl.checks_state("proj/app", "abc"), expected)

    def test_not_applicable_is_dropped_rather_than_left_pending(self):
        # It will never answer, so counting it as pending waits forever.
        impl, _ = azdo({"/statuses": {"value": [
            {"state": "notApplicable", "context": {"name": "skipped"}},
            {"state": "succeeded", "context": {"name": "build"}}]}})
        self.assertEqual(impl.checks(("proj/app"), "abc"),
                         [{"name": "build", "state": forge.GREEN}])
        self.assertEqual(impl.checks_state("proj/app", "abc"), forge.GREEN)

    def test_no_statuses_at_all_is_none(self):
        impl, _ = azdo({"/statuses": {"value": []}})
        self.assertEqual(impl.checks_state("proj/app", "abc"), forge.NONE)


class AWorkItemIsResolvedToARepository(unittest.TestCase):
    """The mapping this host needs and no other one does.

    A work item belongs to a PROJECT. Choosing the wrong repository would put
    a solver to work on the wrong code, so a work item that cannot be placed
    is skipped rather than guessed at.
    """

    def item(self, relations=None):
        return {"id": 7, "fields": {"System.Title": "T"},
                "relations": relations or []}

    def test_a_git_artifact_link_names_the_repository(self):
        impl, _ = azdo()
        link = ("vstfs:///Git/PullRequestId/"
                "22222222-2222-2222-2222-222222222222"
                "%2F11111111-1111-1111-1111-111111111111%2F42")
        self.assertEqual(
            impl._repo_for_work_item(self.item([{"url": link}]), "proj"),
            "proj/app")

    def test_a_project_with_one_repository_is_unambiguous(self):
        impl, _ = azdo()
        self.assertEqual(impl._repo_for_work_item(self.item(), "proj"),
                         "proj/app")

    def test_a_project_with_several_repositories_is_skipped_not_guessed(self):
        impl, _ = azdo()
        impl._repos = {"proj/app": "a", "a": "proj/app",
                       "proj/other": "b", "b": "proj/other"}
        self.assertEqual(impl._repo_for_work_item(self.item(), "proj"), "")


class TheAzureDevOpsHelpersAddressingDependsOn(unittest.TestCase):
    """The small functions every other call on this host is built out of.

    Pinned directly as well as through the public methods: an addressing bug
    here does not raise, it builds a URL for somewhere else, and this host
    answers 404 to a caller that reads 404 as "there is nothing there".
    """

    GUID = "11111111-1111-1111-1111-111111111111"

    def test_split_keeps_the_organisation_out_of_the_repo_name(self):
        # The org is configuration, so `repo` is two segments, not three.
        self.assertEqual(forge.AzureDevOpsForge._split("proj/app"),
                         ("proj", "app"))
        self.assertEqual(forge.AzureDevOpsForge._split(""), ("", ""))

    def test_repo_index_maps_both_directions(self):
        impl, _ = azdo()
        impl._repos = None
        impl._transport = lambda *a, **k: {"value": [
            {"id": self.GUID, "name": "app", "project": {"name": "proj"}}]}
        index = impl._repo_index()
        self.assertEqual(index["proj/app"], self.GUID)
        self.assertEqual(index[self.GUID.lower()], "proj/app")

    def test_repo_index_survives_a_listing_it_cannot_read(self):
        impl, _ = azdo()
        impl._repos = None
        def boom(*a, **k):
            raise forge.ForgeError("nope", code=500)
        impl._transport = boom
        self.assertEqual(impl._repo_index(), {})

    def test_repo_id_and_git_path(self):
        impl, _ = azdo()
        self.assertEqual(impl._repo_id("proj/app"), self.GUID)
        self.assertEqual(impl._repo_id("proj/nope"), "")
        self.assertEqual(
            impl._git("proj/app", "/pullrequests"),
            f"/proj/_apis/git/repositories/{self.GUID}/pullrequests")
        # An unknown repository yields no path at all, so no call is made.
        self.assertEqual(impl._git("proj/nope"), "")

    def test_projects_and_repos_in(self):
        impl, _ = azdo()
        impl._repos = {"proj/app": "a", "a": "proj/app",
                       "proj/other": "b", "b": "proj/other",
                       "two/x": "c", "c": "two/x"}
        self.assertEqual(impl._projects(), ["proj", "two"])
        self.assertEqual(impl._repos_in("proj"), ["proj/app", "proj/other"])

    def test_tags_split_one_string_into_labels(self):
        # Tags are a single semicolon-joined field, not a list.
        self.assertEqual(
            forge.AzureDevOpsForge._tags(
                {"fields": {"System.Tags": "bug; ui ;; done"}}),
            ["bug", "ui", "done"])
        self.assertEqual(forge.AzureDevOpsForge._tags({}), [])

    def test_short_ref_strips_the_qualification(self):
        # Refs are fully qualified on this host; every caller wants the name.
        short = forge_azdo._short_ref
        self.assertEqual(short("refs/heads/topic"), "topic")
        self.assertEqual(short("refs/tags/v1"), "v1")
        self.assertEqual(short("topic"), "topic")
        self.assertEqual(short(None), "")

    def test_patch_fields_uses_the_json_patch_media_type(self):
        impl, t = azdo()
        self.assertTrue(impl._patch_fields("proj", 7, [{"op": "add"}]))
        self.assertIn("/proj/_apis/wit/workitems/7", t.urls("PATCH")[0])

    def test_work_items_asks_for_relations(self):
        # Relations are not in the default projection, and they are what the
        # work-item-to-repository mapping reads.
        impl, t = azdo({"/wit/workitems": {"value": [{"id": 1}]}})
        got = impl._work_items("proj", ["1"])
        self.assertEqual(got, [{"id": 1}])
        self.assertEqual(t.params_for("/wit/workitems")["$expand"], "relations")

    def test_work_items_asks_for_nothing_when_there_are_no_ids(self):
        impl, t = azdo()
        self.assertEqual(impl._work_items("proj", []), [])
        self.assertEqual(t.calls, [])

    def test_reviewer_id_finds_an_identity_on_the_pull_request(self):
        # Reviewers are addressed by identity GUID, and there is no lookup
        # from an account name to one on this route.
        impl, _ = azdo({"/pullrequests/7": {"reviewers": [
            {"id": "abc", "uniqueName": "ann@example.com"}]}})
        self.assertEqual(impl._reviewer_id("proj/app", 7, "ann@example.com"),
                         "abc")
        self.assertEqual(impl._reviewer_id("proj/app", 7, "nobody"), "")


if __name__ == "__main__":
    unittest.main()

"""The translation layer: a host's payload becoming this bot's vocabulary.

WHY THIS IS THE PART WORTH TESTING TWICE
----------------------------------------
Everything else in the seam is plumbing. THIS is where the two hosts actually
differ, and it is the only place a difference is allowed to exist — once a
record has been through here, no caller can tell which host it came from, and
no caller is in a position to get it wrong.

Which also means a mistake here is invisible everywhere else. A field that
translates to the wrong name does not raise; it produces a record that reads
as "no title", "no labels", "not closed" — an answer, of the shape a planner
expects, that is simply untrue. So the two implementations are tested SIDE BY
SIDE from the same starting fact, and the assertions are that they agree.

The four differences pinned here are real and were each found the hard way:

  * `body` is `body` on one host and `description` on the other. The
    destructive-wording guard reads `body`, so getting this wrong disarms it
    silently — the guard sees an empty string and asks nobody.
  * `number` is `number` on one and `iid` on the other. The `id` field also
    exists on the second host, and is a DIFFERENT number, globally unique
    rather than per project. Reading it produces requests for issues that
    exist somewhere else entirely.
  * Labels arrive as objects on one host and bare strings on the other — and
    on the first, some payloads really do return strings anyway.
  * Only one host records WHY an issue was closed. The other has to read the
    intent back out of the status label that was written when it closed.

No network anywhere: the shapes are translated from literals.
"""

import unittest

from harness import load  # noqa: F401 - puts builder/ on sys.path

import forge  # noqa: E402


GITHUB_ISSUE = {
    "number": 42,
    "title": "Add login",
    "body": "the description of the work",
    "html_url": "https://github.com/acme/web/issues/42",
    "labels": [{"name": "bug"}, {"name": "status::in progress"}],
    "state": "open",
    "repository_url": "https://api.github.com/repos/acme/web",
}

GITLAB_ISSUE = {
    "iid": 42,
    # Present, different, and never the one to read.
    "id": 900142,
    "title": "Add login",
    "description": "the description of the work",
    "web_url": "https://gitlab.example.com/acme/web/-/issues/42",
    "labels": ["bug", "status::in progress"],
    "state": "opened",
}


def github():
    return forge.GitHubForge("t", transport=lambda *a, **k: None)


def gitlab():
    return forge.GitLabForge("https://gitlab.example.com", "t",
                             transport=lambda *a, **k: None)


class BothHostsProduceTheSameIssue(unittest.TestCase):
    """`_issue` on either implementation, from the same underlying fact."""

    def test_the_neutral_issue_is_identical_across_hosts(self):
        # The whole seam in one assertion. If this ever fails, a planner is
        # about to make a different decision depending on where an issue
        # lives — which is the bug the interface exists to make impossible.
        a = github()._issue(GITHUB_ISSUE, "acme/web")
        b = gitlab()._issue(GITLAB_ISSUE, "acme/web")
        for field in ("repo", "number", "title", "body", "labels", "state",
                      "closedAs", "author"):
            with self.subTest(field=field):
                self.assertEqual(a[field], b[field])

    def test_each_record_is_stamped_with_the_host_it_came_from(self):
        # `Forges.of` routes on this stamp, so an unstamped record silently
        # goes to whichever host happens to be first.
        self.assertEqual(github()._issue(GITHUB_ISSUE, "acme/web")["forge"],
                         forge.GITHUB)
        self.assertEqual(gitlab()._issue(GITLAB_ISSUE, "acme/web")["forge"],
                         forge.GITLAB)

    def test_the_per_project_number_is_the_one_that_travels(self):
        # `id` on the second host is globally unique and is NOT the number an
        # issue is addressed by. Reading it asks for an issue in some other
        # project, which usually answers 404 and occasionally does not.
        self.assertEqual(gitlab()._issue(GITLAB_ISSUE, "acme/web")["number"],
                         42)

    def test_a_missing_field_becomes_empty_rather_than_none(self):
        # Callers treat body as text: `"drop" in issue["body"]` on None raises
        # inside a planner, in a tick that had a dozen other issues to get to.
        for f, raw in ((github(), {"number": 1}), (gitlab(), {"iid": 1})):
            with self.subTest(forge=f.kind):
                rec = f._issue(raw, "acme/web")
                self.assertEqual(rec["title"], "")
                self.assertEqual(rec["body"], "")
                self.assertEqual(rec["labels"], [])

    def test_an_unstated_state_is_open_on_both(self):
        self.assertEqual(github()._issue({"number": 1}, "r")["state"], "open")
        self.assertEqual(gitlab()._issue({"iid": 1}, "r")["state"], "open")


class ClosingRecordsWhyOnBothHosts(unittest.TestCase):
    """`closedAs`, which only one host has a native field for."""

    def test_a_native_close_reason_is_read_back_as_intent(self):
        f = github()
        self.assertEqual(
            f._issue(dict(GITHUB_ISSUE, state="closed",
                          state_reason="not_planned"), "r")["closedAs"],
            forge.REVOKED)
        self.assertEqual(
            f._issue(dict(GITHUB_ISSUE, state="closed",
                          state_reason="completed"), "r")["closedAs"],
            forge.DELIVERED)

    def test_a_close_with_no_reason_at_all_is_a_delivery(self):
        # Every issue closed before the field existed has none, and all of
        # those shipped. Reading absence as "called off" would retroactively
        # revoke the entire history.
        self.assertEqual(
            github()._issue(dict(GITHUB_ISSUE, state="closed"), "r")["closedAs"],
            forge.DELIVERED)

    def test_without_a_native_field_the_status_label_carries_the_intent(self):
        # This is what `close_issue(delivered=False)` wrote down. Reading it
        # back is the only way the difference survives a close on this host.
        f = gitlab()
        revoked = f._issue(dict(GITLAB_ISSUE, state="closed",
                                labels=["status::wont-do"]), "r")
        delivered = f._issue(dict(GITLAB_ISSUE, state="closed",
                                  labels=["bug"]), "r")
        self.assertEqual(revoked["closedAs"], forge.REVOKED)
        self.assertEqual(delivered["closedAs"], forge.DELIVERED)

    def test_an_open_issue_has_no_close_intent_on_either_host(self):
        # None, not "delivered". An open issue has not been decided, and a
        # caller reading a default here would close the loop on live work.
        self.assertIsNone(github()._issue(GITHUB_ISSUE, "r")["closedAs"])
        self.assertIsNone(gitlab()._issue(GITLAB_ISSUE, "r")["closedAs"])


class LabelsWhateverShapeTheyArriveIn(unittest.TestCase):
    """`_labels`, which exists because one host is not self-consistent."""

    def test_label_objects_become_names(self):
        self.assertEqual(
            github()._labels([{"name": "bug"}, {"name": "SP::5"}]),
            ["bug", "SP::5"])

    def test_bare_strings_are_accepted_from_the_same_host(self):
        # The search payloads really do answer this way. A translator that
        # assumed objects returns [] here — and "no labels" means workable,
        # unparked and unsized to three different gates.
        self.assertEqual(github()._labels(["bug", "SP::5"]), ["bug", "SP::5"])

    def test_blank_and_missing_labels_are_dropped_not_kept_as_empty(self):
        self.assertEqual(github()._labels([{"name": ""}, {}, "  ", None]), [])
        self.assertEqual(github()._labels(None), [])


class AChangeRequestIsNotAStory(unittest.TestCase):
    """`isChangeRequest` — one host serves both from the same collection."""

    def test_a_change_request_read_as_an_issue_says_it_is_one(self):
        # The issues collection on this host returns pull requests too, and
        # they look exactly like issues apart from one extra key. A caller
        # that sizes one is estimating the bot's own output; a caller that
        # plans work on one opens a change request against a change request.
        rec = github()._issue(dict(GITHUB_ISSUE, pull_request={"url": "..."}),
                              "acme/web")
        self.assertTrue(rec["isChangeRequest"])

    def test_an_ordinary_issue_says_it_is_not(self):
        self.assertFalse(github()._issue(GITHUB_ISSUE, "r")["isChangeRequest"])

    def test_the_other_host_answers_the_same_question_and_says_no(self):
        # It keeps merge requests in their own collection, so an issue read is
        # only ever an issue. The field is reported anyway — a caller must be
        # able to ask both hosts the same question rather than knowing which
        # one needs asking.
        rec = gitlab()._issue(GITLAB_ISSUE, "acme/web")
        self.assertIn("isChangeRequest", rec)
        self.assertFalse(rec["isChangeRequest"])


class DefiningALabelBeforeUsingIt(unittest.TestCase):
    """`ensure_label` — because one host will not apply an undefined label.

    The first estimate in a fresh repository fails otherwise, with a status
    that reads like a permissions problem and is not one.
    """

    def calls_of(self, f, transport):
        return [(m, u, j, fo) for (m, u, p, j, fo) in transport.calls]

    def test_a_new_label_is_created_with_the_colour_it_was_given(self):
        seen = []

        def transport(method, url, *, headers, params=None, json_body=None,
                      form_body=None, timeout=None):
            seen.append((method, url, json_body, form_body))
            return {}

        f = forge.GitHubForge("t", transport=transport)
        self.assertTrue(f.ensure_label("acme/web", "SP::5", "c5def5", "five"))
        method, url, body, _ = seen[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/repos/acme/web/labels"))
        self.assertEqual(body, {"name": "SP::5", "color": "c5def5",
                                "description": "five"})

    def test_a_label_that_already_exists_is_success_not_failure(self):
        # After the first estimate in a repository this is EVERY call. A
        # caller that read it as a failure would stop labelling anything the
        # second time round.
        def transport(*a, **k):
            raise forge.ForgeError("422 already_exists", code=422)

        f = forge.GitHubForge("t", transport=transport)
        self.assertTrue(f.ensure_label("acme/web", "SP::5"))

    def test_a_real_failure_is_still_reported(self):
        # Losing the difference would mean a repository the bot cannot write
        # to looks exactly like one where every label is already defined.
        def transport(*a, **k):
            raise forge.ForgeError("403 forbidden", code=403)

        self.assertFalse(
            forge.GitHubForge("t", transport=transport).ensure_label("r", "x"))

    def test_the_other_host_is_given_the_colour_the_way_it_wants_it(self):
        # A CSS hex there, six bare digits here. Exactly the kind of
        # difference no caller should have to know about — and the reason
        # `ensure_label` takes the bot's own vocabulary and translates.
        seen = []

        def transport(method, url, *, headers, params=None, json_body=None,
                      form_body=None, timeout=None):
            seen.append((method, url, form_body))
            return {}

        f = forge.GitLabForge("https://gitlab.example.com", "t",
                              transport=transport)
        self.assertTrue(f.ensure_label("group/app", "SP::5", "c5def5"))
        method, url, fields = seen[0]
        self.assertEqual(fields["color"], "#c5def5")
        self.assertIn("group%2Fapp", url)

    def test_a_label_with_no_name_is_refused_without_a_request(self):
        seen = []
        f = forge.GitHubForge("t", transport=lambda *a, **k: seen.append(a))
        self.assertFalse(f.ensure_label("acme/web", ""))
        self.assertEqual(seen, [])


class NotesAreTheShapeTheGuardsAlreadySpeak(unittest.TestCase):
    """`_note` — author under `username`, on both hosts."""

    def test_the_author_username_lands_in_the_same_place(self):
        # `lexical_guard` and every "has the bot already said this?" check
        # read author.username. One host spells it user.login underneath.
        a = github()._note({"id": 1, "body": "hello",
                            "user": {"login": "cameron-claw"}})
        b = forge.GitLabForge._note({"id": 1, "body": "hello",
                                     "author": {"username": "cameron-claw"}})
        self.assertEqual(a, b)
        self.assertEqual(a["author"]["username"], "cameron-claw")

    def test_an_authorless_note_has_a_username_of_empty_string(self):
        # Not None: every caller compares it to a login, and None compares
        # equal to nothing while also not being a string.
        self.assertEqual(github()._note({"id": 1})["author"]["username"], "")
        self.assertEqual(
            forge.GitLabForge._note({"id": 1})["author"]["username"], "")


class ChangeRequestsAcrossHosts(unittest.TestCase):
    """`_change_request` — including the state that is not in `state`."""

    def test_a_merged_change_request_reads_as_merged_not_closed(self):
        # One host reports a merged pull request as `state: closed` with a
        # separate merged_at. A gate reading `closed` treats delivered work as
        # abandoned and reopens the issue.
        rec = github()._change_request(
            {"number": 7, "state": "closed", "merged_at": "2026-08-01T00:00:00Z",
             "head": {"sha": "abc", "ref": "issue-42-x"},
             "base": {"ref": "main"}}, "acme/web")
        self.assertEqual(rec["state"], "merged")

    def test_the_head_commit_is_where_the_gate_looks(self):
        rec = github()._change_request(
            {"number": 7, "state": "open",
             "head": {"sha": "abc123", "ref": "issue-42-x"},
             "base": {"ref": "main"}}, "acme/web")
        self.assertEqual((rec["headSha"], rec["headRef"], rec["baseRef"]),
                         ("abc123", "issue-42-x", "main"))

    def test_an_open_change_request_reads_as_open_on_both_hosts(self):
        a = github()._change_request(
            {"number": 7, "state": "open", "head": {}, "base": {}}, "r")
        b = gitlab()._change_request(
            {"iid": 7, "state": "opened"}, "r")
        self.assertEqual(a["state"], b["state"])
        self.assertEqual(a["state"], "open")


class AddressingAProject(unittest.TestCase):
    """`_project` and the URL readers — how a repository name is spelled."""

    def test_a_nested_project_path_is_encoded_whole(self):
        # A project lives at group/subgroup/name and the API takes all of it
        # as ONE path segment. Leaving the slashes produces a request for a
        # path that does not exist, which answers 404 — indistinguishable
        # from "no such project" and therefore from "skip this repository".
        self.assertEqual(forge.GitLabForge._project("group/sub/web"),
                         "group%2Fsub%2Fweb")

    def test_the_repository_of_a_listed_item_comes_out_of_its_api_url(self):
        # Listings and searches name the repository only this way.
        self.assertEqual(
            forge._repo_from_api_url("https://api.github.com/repos/acme/web"),
            "acme/web")
        self.assertEqual(forge._repo_from_api_url("nonsense"), "")

    def test_a_project_is_read_out_of_a_browser_url(self):
        base = "https://gitlab.example.com"
        for path in ("/-/issues/42", "/-/work_items/42", "/-/merge_requests/7"):
            with self.subTest(path=path):
                self.assertEqual(
                    forge._project_from_web_url(f"{base}/acme/web{path}", base),
                    "acme/web")

    def test_a_url_that_is_not_ours_yields_nothing_rather_than_a_guess(self):
        # Attaching an item to the wrong project is worse than not attaching
        # it: the bot would then act on a repository nobody asked about.
        base = "https://gitlab.example.com"
        self.assertEqual(forge._project_from_web_url(
            "https://elsewhere.example.com/acme/web/-/issues/42", base), "")
        self.assertEqual(forge._project_from_web_url(
            f"{base}/acme/web/-/snippets/1", base), "")


class RequestsCarryTheirCredentials(unittest.TestCase):
    """`_headers` and `_get` — the two lines every other call goes through."""

    def test_the_token_and_api_version_travel_on_every_request(self):
        # An unversioned request gets whatever the host defaults to, which is
        # how a response shape changes under a bot that never changed.
        h = forge.GitHubForge("secret-token")._headers()
        self.assertEqual(h["Authorization"], "Bearer secret-token")
        self.assertEqual(h["X-GitHub-Api-Version"], "2022-11-28")
        self.assertIn("openclaw", h["User-Agent"])

    def test_the_other_host_authenticates_the_way_it_expects(self):
        h = forge.GitLabForge("https://gitlab.example.com", "secret")._headers()
        self.assertIn("secret", " ".join(str(v) for v in h.values()))

    def test_a_get_is_built_against_the_configured_api_root(self):
        # The root is configurable because self-hosted instances exist, and a
        # hardcoded one silently talks to the wrong installation.
        seen = {}

        def transport(method, url, *, headers, params=None, **kw):
            seen.update(method=method, url=url, params=params)
            return {"ok": True}

        f = forge.GitHubForge("t", api="https://ghe.internal/api/v3",
                              transport=transport)
        self.assertEqual(f._get("/user", {"per_page": 1}), {"ok": True})
        self.assertEqual(seen["url"], "https://ghe.internal/api/v3/user")
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["params"], {"per_page": 1})

    def test_a_trailing_slash_on_the_root_does_not_double_up(self):
        seen = {}

        def transport(method, url, **kw):
            seen["url"] = url
            return None

        forge.GitHubForge("t", api="https://ghe.internal/api/v3/",
                          transport=transport)._get("/user")
        self.assertEqual(seen["url"], "https://ghe.internal/api/v3/user")


class ChoosingBetweenHosts(unittest.TestCase):
    """`Forges` — `__len__`, `__bool__`, `remember` and the routing."""

    def test_no_host_configured_is_falsy_and_empty(self):
        # Callers gate on `if not FORGES:` to say "nothing is configured here"
        # rather than to crash a tick, so both answers have to be right.
        empty = forge.Forges([])
        self.assertEqual(len(empty), 0)
        self.assertFalse(bool(empty))

    def test_the_configured_hosts_are_counted(self):
        both = forge.Forges([github(), gitlab()])
        self.assertEqual(len(both), 2)
        self.assertTrue(bool(both))
        self.assertEqual(both.kinds(), [forge.GITHUB, forge.GITLAB])

    def test_a_remembered_repository_goes_back_to_where_it_was_found(self):
        # The tester's candidates are a list of NAMES from the owner's
        # permitted list — no stamp on them at all. Without remember() they
        # would all be asked of whichever host sorted first, which on a
        # two-host deployment is a request about somebody else's project.
        gh, gl = github(), gitlab()
        forges = forge.Forges([gh, gl])
        forges.remember("acme/web", gl)
        self.assertIs(forges.of("acme/web"), gl)
        self.assertIs(forges.of("never/seen"), gh)

    def test_a_stamped_record_routes_on_its_stamp_not_on_its_name(self):
        gh, gl = github(), gitlab()
        forges = forge.Forges([gh, gl])
        forges.remember("acme/web", gh)
        self.assertIs(forges.of({"repo": "acme/web", "forge": forge.GITLAB}), gl)

    def test_asking_with_nothing_configured_raises_rather_than_no_ops(self):
        # A caller handed "no forge" would do nothing, report nothing, and
        # read in the log exactly like a tick with nothing to do.
        with self.assertRaises(forge.ForgeError):
            forge.Forges([]).of("acme/web")


if __name__ == "__main__":
    unittest.main()

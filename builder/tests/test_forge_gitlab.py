"""GitLab, and the places where it is not like anywhere else.

WHAT THIS FILE IS FOR
---------------------
Several of the worst bugs in this bot were "one host does not behave like the
other", found in production because the difference had no home. Behind the
forge interface each difference belongs to exactly one implementation, and it
can be pinned before anything is deployed. The differences that matter:

  * **A self-review is possible here.** The other host refuses to let an
    author be a reviewer of their own change (422), which is why discovery
    there has to key on authorship. Here BOTH scopes are real, an item can
    honestly appear in both, and the de-duplication is doing work rather than
    guarding against a theoretical overlap.
  * **There is no native close reason.** Nothing on a closed issue says
    whether the work shipped or was called off, so `close_issue(delivered=)`
    has to write the intent down another way — which is exactly why the
    interface takes an intent and not a field.
  * **CI is one pipeline, not two systems.** The four answers have to come out
    the same anyway, `none` still distinct from `pending`, or every caller
    would need to know which host gated it.
  * **The vocabulary is merge requests, and issues carry a `description`.**
    Anything a person reads has to say the right word.

No network: a fake transport stands in for the request.
"""

import unittest

from harness import load  # noqa: F401 - puts builder/ on sys.path

import forge  # noqa: E402
from test_forge_github import FakeTransport  # noqa: E402

BASE = "https://gitlab.example.invalid"


def gl(routes=None):
    t = FakeTransport(routes)
    return forge.GitLabForge(BASE, "token", transport=t), t


def issue_row(iid=4, *, path="group/sub/app", **kw):
    row = {
        "iid": iid,
        "project_id": 12,
        "title": "a task",
        "description": "the body",
        "labels": ["On Hold"],
        "state": "opened",
        "web_url": f"{BASE}/{path}/-/issues/{iid}",
    }
    row.update(kw)
    return row


class Discovery(unittest.TestCase):
    """Assigned work items, keyed by project path."""

    def test_the_record_is_the_same_neutral_shape_as_anywhere_else(self):
        # A planner reads this without knowing which host produced it, so the
        # keys have to match exactly — `description` becomes `body`, `iid`
        # becomes `number`, labels are names.
        f, _ = gl({"/issues": [issue_row()]})
        got = f.assigned_open_issues()["group/sub/app"][0]
        self.assertEqual(got, {
            "forge": forge.GITLAB, "repo": "group/sub/app", "number": 4,
            "title": "a task", "body": "the body",
            "url": f"{BASE}/group/sub/app/-/issues/4",
            "labels": ["On Hold"], "state": "open", "closedAs": None,
            # Same field, this host's spelling of it. See the GitHub half.
            "author": "",
            # Always false here: merge requests live in their own collection,
            # so an issue read is only ever an issue. Present regardless, so
            # a caller asks both hosts the same question.
            "isChangeRequest": False,
            "createdAt": "",
        })

    def test_tasks_are_listed_as_well_as_issues(self):
        # They share the issue iid space and every issue endpoint, so a solver
        # handles them identically — but some versions omit them from the
        # default listing.
        f, t = gl({"/issues": []})
        f.assigned_open_issues()
        types = [p.get("issue_type") for _m, u, p, _j, _f in t.calls
                 if u.endswith("/issues")]
        self.assertIn("task", types)
        self.assertIn(None, types)

    def test_the_same_item_from_both_passes_is_listed_once(self):
        f, _ = gl({"/issues": [issue_row()]})
        self.assertEqual(len(f.assigned_open_issues()["group/sub/app"]), 1)

    def test_a_work_item_url_names_its_project_too(self):
        # Newer versions serve every task, and eventually every issue, under
        # /-/work_items/. Reading only the older spelling silently drops them.
        f, _ = gl({"/issues": [issue_row(
            web_url=f"{BASE}/group/sub/app/-/work_items/9", iid=9)]})
        self.assertIn("group/sub/app", f.assigned_open_issues())

    def test_an_item_from_another_instance_is_not_ours_to_interpret(self):
        f, _ = gl({"/issues": [issue_row(
            web_url="https://elsewhere.invalid/x/y/-/issues/1")]})
        self.assertEqual(f.assigned_open_issues(), {})


class SelfReviewIsPermitted(unittest.TestCase):
    """Both discovery scopes are real here, and that is the difference."""

    def _searching(self, rows):
        f, t = gl({"/user": {"username": "bot"}, "/merge_requests": rows})
        return f, t

    def _scopes(self, t):
        out = []
        for _m, url, params, _j, _f in t.calls:
            if url.endswith("/merge_requests"):
                out.append(params.get("author_username")
                           or params.get("reviewer_username"))
        return out

    def test_it_asks_as_the_author_and_as_the_reviewer(self):
        # The bot CAN be the reviewer of its own merge request here, so being
        # asked for a review is a signal that actually fires — unlike on the
        # other host, where it can never return the bot's own work.
        f, t = self._searching([])
        f.reviewable_change_requests(8)
        self.assertEqual(self._scopes(t), ["bot", "bot"])

    def test_a_merge_request_found_under_both_scopes_is_reviewed_once(self):
        # Authored AND reviewed-by is the NORMAL case here, not a corner one.
        f, _ = self._searching([{
            "iid": 7, "title": "t", "labels": [],
            "web_url": f"{BASE}/g/app/-/merge_requests/7"}])
        items = f.reviewable_change_requests(8)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["repo"], items[0]["number"]), ("g/app", 7))

    def test_every_record_is_stamped_with_the_host_that_found_it(self):
        f, _ = self._searching([{
            "iid": 7, "title": "t", "labels": [],
            "web_url": f"{BASE}/g/app/-/merge_requests/7"}])
        self.assertEqual(f.reviewable_change_requests(8)[0]["forge"],
                         forge.GITLAB)

    def test_no_identity_means_no_search_at_all(self):
        f, t = gl({"/user": {}, "/merge_requests": []})
        self.assertEqual(f.reviewable_change_requests(8), [])
        self.assertEqual([u for u in t.urls() if u.endswith("/merge_requests")],
                         [])


class TerminalState(unittest.TestCase):
    """No close reason exists, so the intent is written down another way."""

    def test_a_revoked_close_is_recorded_before_the_issue_shuts(self):
        f, t = gl()
        self.assertTrue(f.close_issue("g/app", 5, delivered=False))
        method, url, _p, _j, fields = t.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertIn("/issues/5", url)
        self.assertEqual(fields["state_event"], "close")
        self.assertEqual(fields["add_labels"], "status::wont-do")

    def test_a_delivered_close_clears_the_revoking_labels(self):
        # Otherwise a `Won't do` somebody applied earlier would outlive the
        # delivery and read back as a refusal forever.
        f, t = gl()
        f.close_issue("g/app", 5, delivered=True)
        fields = t.calls[-1][4]
        self.assertEqual(fields["state_event"], "close")
        self.assertNotIn("add_labels", fields)
        self.assertIn("status::wont-do", fields["remove_labels"])
        self.assertIn("status::duplicate", fields["remove_labels"])

    def test_the_intent_is_read_back_off_a_closed_issue(self):
        f, _ = gl({"/issues/5": issue_row(
            iid=5, state="closed", labels=["status::wont-do"])})
        self.assertEqual(f.issue("g/app", 5)["closedAs"], forge.REVOKED)

    def test_a_closed_issue_with_no_refusal_label_is_a_delivery(self):
        f, _ = gl({"/issues/5": issue_row(iid=5, state="closed", labels=[])})
        self.assertEqual(f.issue("g/app", 5)["closedAs"], forge.DELIVERED)

    def test_an_open_issue_has_no_terminal_state(self):
        f, _ = gl({"/issues/5": issue_row(iid=5)})
        self.assertIsNone(f.issue("g/app", 5)["closedAs"])


class PipelineReduction(unittest.TestCase):
    """One pipeline, the same four answers."""

    def test_success_is_green(self):
        self.assertEqual(forge.GitLabForge.reduce_pipeline("success"),
                         forge.GREEN)

    def test_a_skipped_pipeline_is_green(self):
        # Same argument as a skipped job elsewhere: a path filter is not a
        # failure, and treating it as one never reviews anything.
        self.assertEqual(forge.GitLabForge.reduce_pipeline("skipped"),
                         forge.GREEN)

    def test_failed_and_canceled_are_failures(self):
        for state in ("failed", "canceled", "cancelled"):
            with self.subTest(state=state):
                self.assertEqual(forge.GitLabForge.reduce_pipeline(state),
                                 forge.FAILED)

    def test_a_manual_pipeline_is_a_failure_not_a_wait(self):
        # It will not run on its own, so waiting for it is waiting forever —
        # the same rule as `action_required` on the other host.
        self.assertEqual(forge.GitLabForge.reduce_pipeline("manual"),
                         forge.FAILED)

    def test_running_and_pending_wait(self):
        for state in ("created", "waiting_for_resource", "preparing",
                      "pending", "running"):
            with self.subTest(state=state):
                self.assertEqual(forge.GitLabForge.reduce_pipeline(state),
                                 forge.PENDING)

    def test_an_unrecognised_status_waits_rather_than_passes(self):
        self.assertEqual(forge.GitLabForge.reduce_pipeline("brand_new_thing"),
                         forge.PENDING)

    def test_no_pipeline_at_all_is_none_and_not_pending(self):
        # A project with no CI configured is not a build in progress. Reading
        # it as pending strands every merge request in that project forever.
        f, _ = gl({"/pipelines": []})
        self.assertEqual(f.checks_state("g/app", "abc"), forge.NONE)

    def test_the_newest_pipeline_is_the_one_that_counts(self):
        f, t = gl({"/pipelines": [{"id": 9, "status": "failed"}]})
        self.assertEqual(f.checks_state("g/app", "abc"), forge.FAILED)
        params = t.params_for("/pipelines")
        self.assertEqual((params["sha"], params["order_by"], params["sort"]),
                         ("abc", "id", "desc"))

    def test_a_failed_lookup_is_pending_never_green(self):
        f, _ = gl({"/pipelines": forge.ForgeError("GitLab 500")})
        self.assertEqual(f.checks_state("g/app", "abc"), forge.PENDING)

    def test_no_sha_is_pending_and_costs_no_call(self):
        f, t = gl()
        self.assertEqual(f.checks_state("g/app", ""), forge.PENDING)
        self.assertEqual(t.calls, [])


class Notes(unittest.TestCase):
    """Comments, minus the bookkeeping the host writes as comments."""

    def test_the_author_is_named_the_same_way_everywhere(self):
        f, _ = gl({"/issues/5/notes": [
            {"id": 1, "body": "hi", "author": {"username": "owner"},
             "created_at": "2026-08-01T10:00:00Z"}]})
        self.assertEqual(f.comments("g/app", 5),
                         [{"id": 1, "body": "hi",
                           "author": {"username": "owner"},
                           "createdAt": "2026-08-01T10:00:00Z"}])

    def test_system_notes_are_not_somebody_speaking(self):
        # Label changes and assignments are recorded as notes authored by
        # whoever caused them. Left in, "a person replied" becomes true the
        # moment the bot itself adds a label — and every park would lift.
        f, _ = gl({"/issues/5/notes": [
            {"id": 1, "body": "added ~On Hold", "system": True,
             "author": {"username": "bot"}},
            {"id": 2, "body": "well?", "author": {"username": "bot"}}]})
        self.assertEqual([n["id"] for n in f.comments("g/app", 5)], [2])

    def test_a_payload_that_is_not_a_list_is_an_error_not_an_empty_thread(self):
        f, _ = gl({"/issues/5/notes": {"message": "boom"}})
        with self.assertRaises(forge.ForgeError):
            f.comments("g/app", 5)


class ChangeRequests(unittest.TestCase):
    def test_the_head_and_the_branches_travel_together(self):
        f, _ = gl({"/merge_requests/7": {
            "iid": 7, "state": "opened", "title": "t", "description": "d",
            "sha": "abc", "source_branch": "issue-5-fix",
            "target_branch": "main", "labels": [],
            "merge_status": "can_be_merged"}})
        cr = f.change_request("g/app", 7)
        self.assertEqual((cr["headSha"], cr["headRef"], cr["baseRef"],
                          cr["state"], cr["mergeable"]),
                         ("abc", "issue-5-fix", "main", "open", True))

    def test_a_draft_is_recognised_from_the_title_as_well_as_the_field(self):
        # Older instances say it only in the title. Reviewing a draft early is
        # a wasted run and a confusing verdict.
        f, _ = gl({"/merge_requests/7": {
            "iid": 7, "state": "opened", "title": "Draft: not ready"}})
        self.assertTrue(f.change_request("g/app", 7)["draft"])

    def test_an_unreadable_change_request_is_empty_not_a_crash(self):
        f, _ = gl({"/merge_requests/7": forge.ForgeError("GitLab 500")})
        self.assertEqual(f.change_request("g/app", 7), {})

    def test_only_open_change_requests_are_linked_to_an_issue(self):
        f, _ = gl({"/related_merge_requests": [
            {"iid": 7, "state": "opened"},
            {"iid": 8, "state": "merged"}]})
        self.assertEqual(f.open_change_requests_for_issue("g/app", 5), [7])

    def test_an_approval_is_the_verdict_this_host_records(self):
        f, _ = gl({"/approvals": {
            "sha": "abc",
            "approved_by": [{"user": {"username": "owner"}}]}})
        self.assertEqual(f.review_verdicts("g/app", 7), [
            {"author": "owner", "verdict": "approved", "body": "", "sha": "abc"}])

    def test_a_merge_squashes_and_removes_the_branch(self):
        f, t = gl()
        self.assertTrue(f.merge("g/app", 7))
        method, url, _p, _j, fields = t.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/merge_requests/7/merge"), url)
        self.assertEqual(fields, {"squash": "true",
                                  "should_remove_source_branch": "true"})


class Addressing(unittest.TestCase):
    """A project is a path, and a path is one encoded segment."""

    def test_a_nested_project_path_is_encoded_whole(self):
        f, t = gl({"/issues/5": issue_row(iid=5)})
        f.issue("group/sub/app", 5)
        self.assertIn("group%2Fsub%2Fapp", t.urls()[0])


class NotYetNeeded(unittest.TestCase):
    """What is not implemented says so, loudly."""

    def test_security_findings_refuses_rather_than_answering_nothing(self):
        # An empty list would read to a caller as "this change is clean",
        # which is a different and much quieter kind of wrong.
        f, _ = gl()
        with self.assertRaises(NotImplementedError) as caught:
            f.security_findings("g/app", 7)
        self.assertIn("not read yet", str(caught.exception))


class Vocabulary(unittest.TestCase):
    def test_the_user_facing_noun_is_a_merge_request(self):
        # It reaches people: a runner that says "pull request" in a project
        # that has none sends the reader looking for something that is not
        # there.
        f, _ = gl()
        self.assertEqual(f.change_request_noun, "merge request")
        self.assertEqual(f.kind, forge.GITLAB)

    def test_the_two_hosts_do_not_share_a_noun(self):
        f, _ = gl()
        self.assertNotEqual(f.change_request_noun,
                            forge.GitHubForge("t").change_request_noun)


class OneHumanOwner(unittest.TestCase):
    """WHO the bot talks to — and why it is never "the owners".

    A project path here starts with a GROUP, and a group inherits Owners from
    every group above it. Splitting the path and calling the first segment the
    owner put one tester run's findings in front of FORTY-TWO people at once:
    assigning a group assigns everybody in it, @-mentioning one notifies
    everybody in it.

    The project record names the one account that CREATED it. That is the
    answer, however many Owners the group has. The member list is consulted
    only when there is no creator to read, and even then exactly one name
    comes back — never the set.
    """

    ENC = "group%2Fsub%2Fapp"

    def owner(self, *, creator=None, user=None, members=None, me="bot"):
        routes = {"/user": {"username": me}}
        routes["%2Fapp"] = {"creator_id": creator} if creator else {}
        if user is not None:
            routes[f"/users/{creator}"] = user
        if members is not None:
            routes["%2Fapp/members/all"] = members
        f, t = gl(routes)
        return f.owner_login("group/sub/app"), t

    @staticmethod
    def member(uid, name, level=50, state="active"):
        return {"id": uid, "username": name, "access_level": level,
                "state": state}

    def test_the_creator_is_the_answer_however_many_owners_there_are(self):
        crowd = [self.member(i, f"person{i}") for i in range(42)]
        who, _t = self.owner(creator=7,
                             user={"username": "ada", "state": "active"},
                             members=crowd)
        self.assertEqual(who, "ada")

    def test_the_answer_is_one_name_and_not_a_list_of_them(self):
        # The shape is the guarantee: a caller cannot accidentally assign a
        # crowd if there is no crowd to assign.
        who, _t = self.owner(creator=7,
                             user={"username": "ada", "state": "active"},
                             members=[self.member(i, f"p{i}") for i in range(42)])
        self.assertIsInstance(who, str)
        self.assertNotIn(",", who)
        self.assertNotIn(" ", who)

    def test_a_creator_who_has_left_falls_back_to_ONE_owner(self):
        # Longest-standing = lowest id. Deterministic, so two ticks address
        # the same person rather than taking turns.
        who, _t = self.owner(creator=7,
                             user={"username": "gone", "state": "blocked"},
                             members=[self.member(9, "carol"),
                                      self.member(3, "bob"),
                                      self.member(5, "dave")])
        self.assertEqual(who, "bob")

    def test_the_bot_is_never_the_person_asked(self):
        # The tester files its own findings. Handing them back to the bot is
        # how work disappears.
        who, _t = self.owner(creator=7,
                             user={"username": "bot", "state": "active"},
                             members=[self.member(1, "bot"),
                                      self.member(8, "erin")])
        self.assertEqual(who, "erin")

    def test_a_maintainer_is_not_an_owner(self):
        who, _t = self.owner(creator=None,
                             members=[self.member(2, "mo", level=40)])
        self.assertEqual(who, "")

    def test_nobody_readable_is_nobody_asked_not_the_group(self):
        # The failure mode this whole method exists to prevent: answering with
        # `group`, which is what the path would have given.
        who, _t = self.owner(creator=None, members=[])
        self.assertEqual(who, "")

    def test_an_unreadable_project_does_not_raise(self):
        f, _t = gl({"/user": {"username": "bot"},
                    "%2Fapp": RuntimeError("500")})
        self.assertEqual(f.owner_login("group/sub/app"), "")

    def test_a_blocked_account_is_not_a_name_to_hand_work_to(self):
        # `_active_username` is the guard: a creator who has left the instance
        # still has an id on the project, and @-mentioning them asks nobody.
        f, _t = gl({"/user": {"username": "bot"},
                    "/users/7": {"username": "gone", "state": "blocked"},
                    "/users/8": {"username": "ada", "state": "active"}})
        self.assertEqual(f._active_username(7), "")
        self.assertEqual(f._active_username(8), "ada")

    def test_the_members_are_asked_for_inherited_ones_too(self):
        # `/members` alone omits the ones a group grants, which on this host
        # is most of them.
        _who, t = self.owner(creator=None,
                             members=[self.member(4, "ana")])
        self.assertTrue(any("/members/all" in u for u in t.urls("GET")),
                        t.urls("GET"))


if __name__ == "__main__":
    unittest.main()

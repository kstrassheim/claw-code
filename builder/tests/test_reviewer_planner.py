"""What the reviewer planner hands to a runner, and what it holds back.

Every entry in this plan costs a checkout, a concurrency slot and a model
turn, and every gate fails the same silent way when it is wrong: the plan
looks reasonable and the bot reviews something it should not have — or stops
reviewing and merely looks idle.

The gates, in the order the planner applies them:

    permission     being asked for a review is a request; the list is the
                   answer, and it is asked before anything is fetched
    lock           one reviewer per repository, because the checkout is shared
    state          a closed or draft change request is not reviewable
    already-seen   same commit AND same prose — see review_subject.py
    checks         green, or a project with no CI at all; never red, never
                   half-finished

No network: the planner reaches its host only through a FAKE FORGE, so not one
of these tests knows or cares which host answered.
"""

import contextlib
import io
import json
import unittest

import fakeforge
import forge
from harness import load_script

rt = load_script("reviewer-tick.py")

REPO = "o/r"


def candidate(number, *, repo=REPO, labels=()):
    return {"forge": forge.GITHUB, "repo": repo, "number": number,
            "title": "a change", "labels": list(labels)}


def change_request(number, *, sha="abc", state="open", draft=False,
                   title="a change", body="", head="issue-1-fix",
                   base="main"):
    return {"forge": forge.GITHUB, "repo": REPO, "number": number,
            "title": title, "body": body, "state": state, "draft": draft,
            "headSha": sha, "headRef": head, "baseRef": base,
            "labels": [], "mergeable": True, "url": ""}


class PlannerTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.forge = fakeforge.FakeForge(identity="bot")
        self.locks = set()
        self.reviewed = {}
        self.perms = {}
        self.allowlist_available = True

        self._saved = {k: getattr(rt, k) for k in
                       ("FORGES", "k8s_namespace", "find_openclaw_pod",
                        "query_pod_state", "query_permissions")}
        rt.FORGES = forge.Forges([self.forge])
        rt.k8s_namespace = lambda: "claw-code-local"
        rt.find_openclaw_pod = lambda ns: "openclaw-0"
        rt.query_pod_state = lambda ns, pod: (set(self.locks),
                                              dict(self.reviewed))
        rt.query_permissions = lambda ns, pod, repos: (
            {r: self.perms.get(r, 0) for r in repos}, self.allowlist_available)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(rt, k, v)

    def plan(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rt.main()
        return json.loads(out.getvalue())

    def entry(self, plan, number):
        for e in plan["prs"]:
            if e["prNumber"] == number:
                return e
        raise AssertionError(f"{number} missing from {plan['prs']}")


class TheGreenGate(PlannerTestCase):
    def test_a_green_head_is_spawned(self):
        self.forge.candidates = [candidate(7)]
        self.forge.change_requests = {7: change_request(7)}
        self.forge.checks["abc"] = forge.GREEN
        entry = self.entry(self.plan(), 7)
        self.assertTrue(entry["toSpawn"])
        self.assertEqual(entry["headSha"], "abc")

    def test_a_project_with_no_ci_at_all_is_still_reviewed(self):
        # Waiting for checks that will never be created would strand every
        # change request in that project forever.
        self.forge.candidates = [candidate(7)]
        self.forge.change_requests = {7: change_request(7)}
        self.forge.checks["abc"] = forge.NONE
        self.assertTrue(self.entry(self.plan(), 7)["toSpawn"])

    def test_a_red_head_is_not_reviewed(self):
        self.forge.candidates = [candidate(7)]
        self.forge.change_requests = {7: change_request(7)}
        self.forge.checks["abc"] = forge.FAILED
        entry = self.entry(self.plan(), 7)
        self.assertFalse(entry["toSpawn"])
        self.assertEqual(entry["reason"], "checks-failed")

    def test_an_unfinished_build_waits(self):
        # And `pending` is a different answer from `none`: one becomes green
        # on its own and the other never will.
        self.forge.candidates = [candidate(7)]
        self.forge.change_requests = {7: change_request(7)}
        self.forge.checks["abc"] = forge.PENDING
        self.assertEqual(self.entry(self.plan(), 7)["reason"], "wait-checks")


class TheOtherGates(PlannerTestCase):
    def test_a_repository_that_is_not_permitted_costs_no_fetch(self):
        # Being asked for a review is a request. The list is the answer, and
        # it is consulted before anything is paid for.
        self.forge.candidates = [candidate(7)]
        self.perms[REPO] = 2
        self.forge.raises["change_request"] = AssertionError(
            "a refused repository must not be fetched")
        entry = self.entry(self.plan(), 7)
        self.assertFalse(entry["toSpawn"])
        self.assertEqual(entry["reason"], "not-permitted")

    def test_an_unanswerable_permission_check_refuses_too(self):
        self.forge.candidates = [candidate(7)]
        self.perms[REPO] = 1
        self.assertEqual(self.entry(self.plan(), 7)["reason"],
                         "allowlist-unavailable")

    def test_a_live_lock_blocks_a_second_reviewer(self):
        self.forge.candidates = [candidate(7)]
        self.locks = {"o__r"}
        self.assertEqual(self.entry(self.plan(), 7)["reason"], "lock-held")

    def test_one_reviewer_per_repository_per_tick(self):
        # The review checkout is shared, so two runners on one repository
        # would race in the same working tree.
        self.forge.candidates = [candidate(7), candidate(8)]
        self.forge.change_requests = {7: change_request(7, sha="a"),
                                      8: change_request(8, sha="b")}
        self.forge.checks.update({"a": forge.GREEN, "b": forge.GREEN})
        plan = self.plan()
        self.assertEqual([e["prNumber"] for e in plan["prs"] if e["toSpawn"]],
                         [7])
        self.assertEqual(self.entry(plan, 8)["reason"], "lock-held")

    def test_a_draft_is_not_reviewed(self):
        self.forge.candidates = [candidate(7)]
        self.forge.change_requests = {7: change_request(7, draft=True)}
        self.assertEqual(self.entry(self.plan(), 7)["reason"], "draft")

    def test_a_closed_change_request_is_not_reviewed(self):
        self.forge.candidates = [candidate(7)]
        self.forge.change_requests = {7: change_request(7, state="merged")}
        self.assertEqual(self.entry(self.plan(), 7)["reason"], "closed")

    def test_an_unreadable_change_request_is_reported_not_guessed(self):
        self.forge.candidates = [candidate(7)]
        self.forge.change_requests = {}
        self.assertEqual(self.entry(self.plan(), 7)["reason"],
                         "pr-fetch-failed")


class AlreadyReviewed(PlannerTestCase):
    def setUp(self):
        super().setUp()
        self.review_subject = load_script("reviewer-tick.py").review_subject
        self.forge.candidates = [candidate(7)]
        self.forge.change_requests = {7: change_request(7, title="t",
                                                        body="b")}
        self.forge.checks["abc"] = forge.GREEN

    def test_the_same_commit_and_prose_is_skipped(self):
        self.reviewed["o__r__7"] = self.review_subject.stamp("abc", "t", "b")
        self.assertEqual(self.entry(self.plan(), 7)["reason"],
                         "already-reviewed")

    def test_an_edited_description_earns_another_look(self):
        # The regression the prose digest exists for: the reviewer asked for a
        # line to go, the author removed it, and the commit did not move.
        self.reviewed["o__r__7"] = self.review_subject.stamp("abc", "t",
                                                             "something else")
        self.assertTrue(self.entry(self.plan(), 7)["toSpawn"])


class Ordering(PlannerTestCase):
    def test_the_more_urgent_change_request_takes_the_repositorys_slot(self):
        # Sorted BEFORE the per-repository claim, not after: the claim is
        # first-come, so sorting the output alone would look right in the plan
        # and behave wrong.
        self.forge.candidates = [candidate(7, labels=["Priority::Low"]),
                                 candidate(8, labels=["Priority::Very High"])]
        self.forge.change_requests = {7: change_request(7, sha="a"),
                                      8: change_request(8, sha="b")}
        self.forge.checks.update({"a": forge.GREEN, "b": forge.GREEN})
        plan = self.plan()
        self.assertEqual([e["prNumber"] for e in plan["prs"] if e["toSpawn"]],
                         [8])

    def test_the_plan_names_the_priority_it_sorted_on(self):
        self.forge.candidates = [candidate(7, labels=["priority::high"])]
        self.forge.change_requests = {7: change_request(7)}
        self.assertEqual(self.entry(self.plan(), 7)["priority"], "High")


class TheCheckGate(PlannerTestCase):
    """`head_check_state` — the planner's one question about CI.

    It delegates, and that is the whole content of it: the reduction from a
    host's own vocabulary to green/failed/pending/none lives in the forge and
    nowhere else. A planner that re-derived it would be a second opinion about
    whether a change request is safe to look at, and the two would disagree
    the first time a host added a conclusion nobody here had heard of.
    """

    def test_it_answers_with_what_the_host_reduced_the_checks_to(self):
        self.forge.checks["abc"] = forge.GREEN
        self.assertEqual(rt.head_check_state(REPO, "abc"), forge.GREEN)

    def test_a_commit_with_no_checks_is_not_the_same_as_one_still_running(self):
        # `none` and `pending` lead to opposite decisions — review it now
        # versus wait — so the planner must be able to tell them apart.
        self.forge.checks.update({"nothing": forge.NONE, "waiting": forge.PENDING})
        self.assertEqual(rt.head_check_state(REPO, "nothing"), forge.NONE)
        self.assertEqual(rt.head_check_state(REPO, "waiting"), forge.PENDING)
        self.assertNotEqual(forge.NONE, forge.PENDING)

    def test_the_question_goes_to_the_host_the_repository_belongs_to(self):
        other = fakeforge.FakeForge(forge.GITLAB, identity="bot")
        other.checks["abc"] = forge.FAILED
        self.forge.checks["abc"] = forge.GREEN
        rt.FORGES = forge.Forges([self.forge, other])
        rt.FORGES.remember("group/app", other)
        self.assertEqual(rt.head_check_state("group/app", "abc"), forge.FAILED)


class NothingConfigured(PlannerTestCase):
    def test_a_deployment_with_no_credentials_says_so(self):
        rt.FORGES = forge.Forges([])
        plan = self.plan()
        self.assertEqual(plan["prs"], [])
        self.assertIn("no code host", plan["error"])


if __name__ == "__main__":
    unittest.main()

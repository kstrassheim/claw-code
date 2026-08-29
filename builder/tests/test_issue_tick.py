"""What the issue planner is allowed to hand to a solver, and in what order.

WHY THESE AND NOT THE HTTP
--------------------------
Every gate in heartbeat-issue-tick decides whether a repository checkout, a
concurrency slot and a model turn get spent. Each of them fails in the same
shape when it is wrong: nothing crashes, the plan looks plausible, and the bot
works on something nobody asked it to — or stops working on everything and
looks merely idle.

So the tests here drive `main()` with the network replaced and assert on the
PLAN, which is the artefact the spawner acts on. Four gates, in the order the
planner applies them:

    allowlist   permission, before any per-issue call is even paid for
    status      Done / Won't do / Duplicate are finished or somebody's call
    On Hold     a question is pending; only a human takes the label off
    lexical     destructive wording is asked about before it is worked

and then the ordering rules, which decide WHICH issue a repo's single slot
goes to.

No network and no pod: the planner talks to a FAKE FORGE, and the kubectl
exec is replaced. The exec stub answers by inspecting the script it was
handed, so the allowlist read and the queue publish are exercised for real
rather than stubbed away.
"""

import contextlib
import io
import json
import unittest

import fakeforge
import forge
from harness import load_script

tick = load_script("heartbeat-issue-tick.py")

ALLOWED = "o/r"


def issue(number, *, title="a task", labels=(), state="open",
          closed_as=None, body="", repo=ALLOWED):
    """One issue in the shape a forge hands back — see forge.py."""
    return {
        "forge": forge.GITHUB,
        "repo": repo,
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        # How a close was recorded, in intent: the work shipped, or it was
        # called off. Never a host's own field.
        "closedAs": closed_as,
        "url": f"https://example.invalid/{repo}/issues/{number}",
        "labels": list(labels),
    }


class PlannerTestCase(unittest.TestCase):
    """A planner tick with the pod and GitHub replaced."""

    maxDiff = None

    def setUp(self):
        self.allowlist_text = ALLOWED + "\n"
        self.allowlist_readable = True
        self.locked = set()
        self.published = []         # every queue-state snippet handed to exec

        self.forge = fakeforge.FakeForge(identity="bot")

        self._saved = {
            k: getattr(tick, k) for k in
            ("kubectl_exec_capture", "k8s_find_openclaw_pod", "_read",
             "FORGES", "MAX_PER_REPO")
        }
        tick.kubectl_exec_capture = self._exec
        tick.k8s_find_openclaw_pod = lambda ns: "openclaw-0"
        tick._read = lambda path: "claw-code-local"
        tick.FORGES = forge.Forges([self.forge])

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(tick, k, v)

    # -- what the forge is holding --------------------------------------

    @property
    def issues(self):
        return self.forge.issues

    @issues.setter
    def issues(self, rows):
        self.forge.issues = list(rows)

    @property
    def comments(self):
        """Issue number -> notes, as the forge would hand them back."""
        return self.forge.notes

    @property
    def posted(self):
        """Everything the planner asked the forge to write, in order."""
        return self.forge.writes

    # -- the fakes ------------------------------------------------------

    def _exec(self, namespace, pod, *cmd, timeout=15):
        """Answer the way the pod would, keyed on what was asked."""
        script = cmd[-1]
        if tick.project_allowlist.ALLOWED_MARKER in script:
            if not self.allowlist_readable:
                return (1, "", "exec failed")
            return (0, tick.project_allowlist.ALLOWED_MARKER + "\n"
                    + self.allowlist_text, "")
        if ".fixer-locks" in script:
            return (0, "\n".join(sorted(self.locked)), "")
        if "issue-markers" in script:
            # The awaiting-review / awaiting-human markers the planner ranks
            # on. None here: these tests are about the gates, and the ordering
            # they imply is pinned in test_wait_bounds against a real bash and
            # real mtimes, because the TTL lives inside the snippet.
            return (0, "", "")
        if "queue-state" in script:
            self.published.append(script)
            return (0, "", "")
        raise AssertionError(f"unexpected exec: {script[:80]}")

    # -- driving it -----------------------------------------------------

    def plan(self, max_per_repo=1):
        tick.MAX_PER_REPO = max_per_repo
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = tick.main()
        self.assertEqual(rc, 0, out.getvalue())
        return json.loads(out.getvalue())

    def repo_entry(self, plan, repo=ALLOWED):
        for r in plan["repos"]:
            if r["repo"] == repo:
                return r
        raise AssertionError(f"{repo} missing from {plan['repos']}")

    def spawned(self, plan, repo=ALLOWED):
        return [e["issueNumber"] for e in self.repo_entry(plan, repo)["toSpawn"]]


class AllowlistGate(PlannerTestCase):
    """Being assigned an issue is a request. This list is the answer."""

    def test_a_permitted_repo_is_planned(self):
        self.issues = [issue(1)]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [1])
        self.assertNotIn("reason", self.repo_entry(plan))

    def test_a_repo_not_on_the_list_spawns_nothing(self):
        self.issues = [issue(1, repo="stranger/repo")]
        plan = self.plan()
        entry = self.repo_entry(plan, "stranger/repo")
        self.assertEqual(entry["toSpawn"], [])
        self.assertEqual(entry["reason"], "not-permitted")

    def test_an_empty_list_is_reported_as_empty_not_as_a_refusal(self):
        # "The owner has permitted nothing" and "this repo was not chosen" are
        # different operational situations and the spawner reports them apart.
        self.allowlist_text = "# nothing granted yet\n"
        self.issues = [issue(1)]
        plan = self.plan()
        self.assertEqual(self.repo_entry(plan)["reason"], "allowlist-empty")
        self.assertEqual(plan["allowedProjects"], 0)

    def test_an_unreadable_list_permits_nothing_and_says_so(self):
        # Fail CLOSED, and be loud about which failure it was: falling back to
        # "everything" would turn a read error into exactly the unrestricted
        # behaviour the list exists to prevent.
        self.allowlist_readable = False
        self.issues = [issue(1)]
        plan = self.plan()
        self.assertIs(plan["allowlistAvailable"], False)
        self.assertIsNone(plan["allowedProjects"])
        self.assertEqual(self.repo_entry(plan)["reason"], "allowlist-unavailable")
        self.assertEqual(self.repo_entry(plan)["toSpawn"], [])

    def test_a_refused_repo_is_never_asked_about(self):
        # The gate is first for a reason: a repo the owner did not permit must
        # not cost a single per-issue call, and must never be written to.
        self.issues = [issue(1, repo="stranger/repo",
                             body="remove the tests, they slow us down")]
        self.plan()
        self.assertEqual(self.posted, [])


class StatusFilter(PlannerTestCase):
    """Only `To do` and `In progress` are a planner's to pick up."""

    def test_to_do_is_workable(self):
        self.issues = [issue(1)]
        self.assertEqual(self.spawned(self.plan()), [1])

    def test_in_progress_is_workable(self):
        self.issues = [issue(1, labels=["status::in-progress"])]
        self.assertEqual(self.spawned(self.plan()), [1])

    def test_wont_do_is_not_planned_even_while_the_issue_is_open(self):
        # A human's terminal call. The issue is still open — GitHub has no
        # other way to say it — and re-planning it is how a bot re-opens work
        # somebody deliberately ended.
        self.issues = [issue(1, labels=["status::wont-do"])]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.repo_entry(plan)["notWorkable"], 1)

    def test_duplicate_is_not_planned(self):
        self.issues = [issue(1, labels=["status::duplicate"])]
        self.assertEqual(self.spawned(self.plan()), [])

    def test_a_closed_issue_is_not_planned(self):
        # The lister asks for open issues only, but the status gate must not
        # depend on that: a state that changed between the fetch and the plan
        # would otherwise spawn a solver on a delivered story.
        self.issues = [issue(1, state="closed", closed_as="delivered")]
        self.assertEqual(self.spawned(self.plan()), [])

    def test_an_unknown_status_label_does_not_wedge_the_rest_of_the_tick(self):
        # A `status::` label naming nothing we recognise resolves to To do
        # rather than raising. A planner that raises on one mislabelled issue
        # stops planning every OTHER issue in the same tick.
        self.issues = [issue(1, labels=["status::whatever"]), issue(2)]
        self.assertEqual(self.spawned(self.plan(max_per_repo=5)), [1, 2])


class OnHoldParking(PlannerTestCase):
    """A question is pending. Only a human hands the issue back."""

    def test_an_on_hold_issue_is_not_spawned(self):
        self.issues = [issue(1, labels=["On Hold"])]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.repo_entry(plan)["onHold"], 1)

    def test_the_label_is_matched_case_insensitively_and_through_a_scope(self):
        # It is applied by hand, and nobody types a label the same way twice.
        for spelling in ("on hold", "ON HOLD", "On-Hold", "Status::On Hold",
                         "on_hold"):
            with self.subTest(spelling=spelling):
                self.issues = [issue(1, labels=[spelling])]
                self.assertEqual(self.spawned(self.plan()), [],
                                 f"{spelling!r} did not park the issue")

    def test_removing_the_label_hands_the_issue_back(self):
        # The release is the whole design: On Hold must not be a one-way trap.
        self.issues = [issue(1, labels=["On Hold"])]
        self.assertEqual(self.spawned(self.plan()), [])
        self.issues = [issue(1)]
        self.assertEqual(self.spawned(self.plan()), [1])

    def test_a_similar_label_does_not_park_anything(self):
        # "onboarding" contains neither word; the fold must not be so eager
        # that an ordinary label silently stops work.
        self.issues = [issue(1, labels=["onboarding"]), issue(2, labels=["hold"])]
        self.assertEqual(self.spawned(self.plan(max_per_repo=5)), [1, 2])

    def test_a_parked_issue_is_not_counted_as_pending_work(self):
        # queue_state feeds the tester's "solve and merge first" rule. An issue
        # waiting on a human is not pending work, and counting it as such would
        # disable deployment testing indefinitely.
        self.issues = [issue(1, labels=["On Hold"])]
        plan = self.plan()
        self.assertEqual(plan["pendingIssues"], 0)


class PriorityOrder(PlannerTestCase):
    """Priority orders what is workable. It never overrides what is in flight."""

    def test_the_more_urgent_issue_takes_the_repos_single_slot(self):
        self.issues = [issue(1, labels=["Priority::Low"]),
                       issue(2, labels=["Priority::Very High"])]
        self.assertEqual(self.spawned(self.plan()), [2])

    def test_equal_priority_is_oldest_first(self):
        self.issues = [issue(9), issue(4), issue(7)]
        self.assertEqual(self.spawned(self.plan(max_per_repo=3)), [4, 7, 9])

    def test_an_unlabelled_issue_sits_between_high_and_low(self):
        self.issues = [issue(1, labels=["Priority::High"]),
                       issue(2),
                       issue(3, labels=["Priority::Very Low"])]
        self.assertEqual(self.spawned(self.plan(max_per_repo=3)), [1, 2, 3])

    def test_work_already_in_progress_outranks_a_more_urgent_fresh_issue(self):
        # The in-flight rule exists so the bot converges on one issue instead
        # of leaving a trail of half-finished branches. A label must not undo
        # it — that is the whole distinction issue_priority documents.
        self.issues = [issue(1, labels=["status::in-progress", "Priority::Low"]),
                       issue(2, labels=["Priority::Very High"])]
        self.assertEqual(self.spawned(self.plan()), [1])

    def test_the_plan_names_the_priority_it_sorted_on(self):
        self.issues = [issue(1, labels=["priority::high"])]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertEqual(entry["priority"], "High")


class EstimationRouting(PlannerTestCase):
    """Size first, implement next tick — the model depends on the size."""

    def test_an_unsized_issue_asks_to_be_estimated(self):
        self.issues = [issue(1)]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertIs(entry["needsEstimate"], True)
        self.assertIs(entry["pointsDefaulted"], True)
        self.assertEqual(entry["storyPoints"], 8)

    def test_a_sized_issue_carries_its_points_to_the_solver(self):
        self.issues = [issue(1, labels=["SP::3"])]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertIs(entry["needsEstimate"], False)
        self.assertIs(entry["pointsDefaulted"], False)
        self.assertEqual(entry["storyPoints"], 3)

    def test_an_estimate_request_re_sizes_an_already_sized_issue(self):
        # The label is a REQUEST, not a result: it means "size this again".
        self.issues = [issue(1, labels=["SP::3", "estimate"])]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertIs(entry["needsEstimate"], True)

    def test_a_hand_typed_size_scope_is_read_rather_than_re_estimated(self):
        self.issues = [issue(1, labels=["story points::5"])]
        entry = self.repo_entry(self.plan())["toSpawn"][0]
        self.assertIs(entry["needsEstimate"], False)
        self.assertEqual(entry["storyPoints"], 5)


class NextSprintDeferral(PlannerTestCase):
    """`next sprint` parks the implementation. It used to park nothing.

    The label was read by estimate-runner and by no other caller, so the
    planner spawned a fixer for a deferred issue exactly as if it were not
    labelled at all — and the bot implemented work a person had explicitly
    put off.
    """

    def test_a_sized_deferred_issue_is_not_spawned(self):
        self.issues = [issue(1, labels=["SP::3", "next sprint"])]
        self.assertEqual(self.spawned(self.plan()), [])

    def test_the_deferral_is_counted_rather_than_looking_idle(self):
        # A tick that spawned nothing has to say WHICH gate stopped it.
        self.issues = [issue(1, labels=["SP::3", "next sprint"])]
        entry = self.repo_entry(self.plan())
        self.assertEqual(entry["nextSprint"], 1)
        self.assertEqual(entry["openAssignedCount"], 0)

    def test_an_unsized_deferred_issue_is_still_estimated(self):
        # The deferral parks the WORK, not the number: next sprint's planning
        # needs the size in order to decide. Same rule estimate-runner states
        # from the other side.
        self.issues = [issue(1, labels=["next sprint"])]
        spawn = self.repo_entry(self.plan())["toSpawn"]
        self.assertEqual([e["issueNumber"] for e in spawn], [1])
        self.assertIs(spawn[0]["needsEstimate"], True)

    def test_it_is_the_same_instruction_however_it_is_typed(self):
        for spelling in ("Next Sprint", "next-sprint", "next_sprint",
                         "NextSprint", "nextsprint", "plan::Next Sprint"):
            with self.subTest(spelling):
                self.issues = [issue(1, labels=["SP::3", spelling])]
                self.assertEqual(self.spawned(self.plan()), [])

    def test_a_similar_label_defers_nothing(self):
        for spelling in ("sprint", "next", "sprint::4",
                         "next sprint planning"):
            with self.subTest(spelling):
                self.issues = [issue(1, labels=["SP::3", spelling])]
                self.assertEqual(self.spawned(self.plan()), [1])

    def test_taking_the_label_off_hands_the_issue_back(self):
        self.issues = [issue(1, labels=["SP::3"])]
        self.assertEqual(self.spawned(self.plan()), [1])

    def test_a_deferred_issue_does_not_hold_the_repos_slot(self):
        # MAX_PER_REPO is 1. A deferral that merely ranked last would still
        # take the slot and stall everything behind it.
        self.issues = [issue(1, labels=["SP::3", "next sprint"]),
                       issue(2, labels=["SP::3"])]
        self.assertEqual(self.spawned(self.plan()), [2])


class QueuePublication(PlannerTestCase):
    """The tester holds off while there is anything left to solve."""

    def test_the_post_gate_count_is_published(self):
        self.issues = [issue(1), issue(2), issue(3, labels=["status::wont-do"])]
        plan = self.plan()
        self.assertEqual(plan["pendingIssues"], 2)
        self.assertEqual(len(self.published), 1)
        self.assertIn("queue-state/solver", self.published[0])
        self.assertIn("2 ", self.published[0])

    def test_a_repo_that_is_not_permitted_contributes_nothing(self):
        self.issues = [issue(1, repo="stranger/repo"), issue(2)]
        self.assertEqual(self.plan()["pendingIssues"], 1)

    def test_a_failed_publish_does_not_fail_the_tick(self):
        # A stale marker reads as unknown to the tester, which then runs. That
        # is the safe direction; losing the whole plan is not.
        def boom(*a, **kw):
            if "queue-state" in a[-1]:
                raise RuntimeError("exec died")
            return self._exec(*a, **kw)
        tick.kubectl_exec_capture = boom
        self.issues = [issue(1)]
        self.assertEqual(self.spawned(self.plan()), [1])


class AskBeforeSpawning(PlannerTestCase):
    """Destructive wording is put to a human before a solver is started."""

    DESTRUCTIVE = "Please remove the test suite, it slows down the build."

    def test_a_destructive_issue_is_questioned_instead_of_spawned(self):
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.repo_entry(plan)["awaitingConfirmation"], 1)

    def test_the_question_itself_is_posted(self):
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        self.plan()
        bodies = self.forge.writes_of("comment")
        self.assertEqual(len(bodies), 1, self.posted)
        self.assertIn(tick.lexical_guard.ASK_MARKER, bodies[0])

    def test_the_issue_is_parked_so_the_question_is_not_re_asked_every_tick(self):
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        self.plan()
        self.assertEqual(self.forge.writes_of("labels"), [["On Hold"]])

    def test_a_question_already_on_the_record_is_not_asked_twice(self):
        self.issues = [issue(1, body=self.DESTRUCTIVE, labels=["On Hold"])]
        self.comments[1] = [fakeforge.note(
            f"🛑 {tick.lexical_guard.ASK_MARKER}\n\nwell?", "bot", 5)]
        # Parked, so it never even reaches the guard — and nothing is posted.
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.posted, [])

    def test_an_unanswered_question_keeps_the_issue_out_of_the_queue(self):
        # The label was taken off but the answer never came: the guard still
        # sees its own note and declines to spawn, rather than asking again.
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        self.comments[1] = [fakeforge.note(
            f"🛑 {tick.lexical_guard.ASK_MARKER}\n\nwell?", "bot", 5)]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [])
        self.assertEqual(self.posted, [])

    def test_ordinary_wording_is_spawned_untouched(self):
        self.issues = [issue(1, body="Add a dark mode toggle to the settings page.")]
        self.assertEqual(self.spawned(self.plan()), [1])
        self.assertEqual(self.posted, [])

    def test_a_prohibition_is_not_read_as_a_request(self):
        # "must not delete the tests" is the issue telling you NOT to.
        self.issues = [issue(1, body="The change must not delete any tests.")]
        self.assertEqual(self.spawned(self.plan()), [1])
        self.assertEqual(self.posted, [])

    def test_an_unpostable_question_still_stops_the_spawn(self):
        # An issue that should have been questioned and could not be is an
        # issue to leave alone — the safe direction is the one that does no
        # work.
        self.forge.writes_fail = True
        self.issues = [issue(1, body=self.DESTRUCTIVE)]
        self.assertEqual(self.spawned(self.plan()), [])


class TwoHostsInOneTick(PlannerTestCase):
    """Which host answers is decided per issue, not per deployment.

    The property a global switch cannot have, and the reason the seam exists:
    a repository on each host is planned in the SAME tick, and every question
    about either one goes back to the host that found it.
    """

    def setUp(self):
        super().setUp()
        self.other = fakeforge.FakeForge(forge.GITLAB, identity="bot",
                                         noun="merge request")
        tick.FORGES = forge.Forges([self.forge, self.other])
        self.allowlist_text = "o/r\ngroup/app\n"

    def test_both_hosts_are_planned_in_one_tick(self):
        self.forge.issues = [issue(1)]
        self.other.issues = [{**issue(2, repo="group/app"),
                              "forge": forge.GITLAB}]
        plan = self.plan()
        self.assertEqual(self.spawned(plan), [1])
        self.assertEqual(self.spawned(plan, "group/app"), [2])

    def test_forge_of_routes_an_issue_by_the_stamp_discovery_left_on_it(self):
        # `forge_of` is what every write in this tick goes through. Routing on
        # the repository NAME alone is not enough: names are unique within a
        # host and not across them, so two projects called `group/app` on two
        # hosts are one key. The stamp is the only thing that tells them apart.
        gh_issue = issue(1)
        gl_issue = {**issue(2, repo="group/app"), "forge": forge.GITLAB}
        self.assertIs(tick.forge_of(gh_issue), self.forge)
        self.assertIs(tick.forge_of(gl_issue), self.other)

    def test_forge_of_falls_back_to_the_first_host_for_an_unknown_name(self):
        # A bare repository name that discovery never saw — the tester's
        # candidates arrive this way. One host configured makes this the only
        # possible answer; two makes it a guess, and the guess is logged
        # rather than silently correct.
        self.assertIs(tick.forge_of("never/discovered"), self.forge)

    def test_the_question_is_asked_on_the_host_that_found_the_issue(self):
        # A write sent to the wrong host is a comment on somebody else's
        # project — or, more usually, a 404 and a silently unasked question.
        self.other.issues = [{**issue(2, repo="group/app",
                                      body="Please remove the test suite."),
                              "forge": forge.GITLAB}]
        self.plan()
        self.assertEqual(self.forge.writes, [])
        self.assertEqual([w[0] for w in self.other.writes],
                         ["comment", "labels"])

    def test_one_host_answering_nothing_does_not_stop_the_other(self):
        self.forge.issues = [issue(1)]
        self.other.issues = []
        self.assertEqual(self.spawned(self.plan()), [1])


if __name__ == "__main__":
    unittest.main()

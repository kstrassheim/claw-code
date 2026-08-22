"""What the tester planner decides before anything expensive happens.

THREE DECISIONS, ALL OF THEM SILENT WHEN THEY GO WRONG
------------------------------------------------------
1. **Which repositories are candidates at all.** The planner used to ask
   GitHub for the repositories the bot can reach, sorted by push date and
   capped. A permitted repository that is simply QUIET then sits below the cut
   and is never tested — and "never tested" looks exactly like "tested, found
   nothing". The permitted list is the candidate list instead, and discovery
   survives only as the path taken when that list cannot be read.

2. **Whether the repository is permitted**, asked of `project-allow check`
   inside the pod that holds the list. Exit 2 is an answer ("not permitted");
   anything else means we could not find out, which permits nothing.

3. **Whether to run at all this tick.** Testing is the most expensive of the
   three subsystems and the least urgent, so it waits while the solver or the
   reviewer still have queued work. The direction of the STALE case is the
   part worth pinning: a marker nobody refreshed is UNKNOWN, and unknown must
   let the tester run. "No news means blocked" would turn one crashed planner
   into a permanent, silent shutdown of deployment testing.

No network and no kubectl: every call that would leave the process is
replaced.
"""

import contextlib
import io
import json
import time
import unittest

from harness import load, load_script

tt = load_script("tester-tick.py")
queue_state = load("queue_state")
from project_allowlist import Allowlist  # noqa: E402


class _Patched:
    """Swap module attributes for the duration of a `with` block."""

    def __init__(self, module, **kw):
        self.module, self.kw, self.old = module, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.module, k)
            setattr(self.module, k, v)
        return self.module

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.module, k, v)
        return False


def fresh(count, age=0):
    """A queue marker written `age` seconds ago."""
    return (count, int(time.time()) - age)


class CandidateList(unittest.TestCase):
    """The allowed-projects list IS the candidate list."""

    def test_the_permitted_repositories_are_the_candidates(self):
        allowed = Allowlist(["octocat/quiet-one", "acme/web"])
        with _Patched(tt, discover_repos=lambda: self.fail(
                "discovery must not run when the permitted list is readable")):
            self.assertEqual(tt.candidate_repos(allowed),
                             ["octocat/quiet-one", "acme/web"])

    def test_a_quiet_permitted_repository_is_still_a_candidate(self):
        # The regression this replaced: /user/repos?sort=pushed returns the
        # most recently active repositories, so a permitted repository nobody
        # pushed to lately never appeared in the plan — silently, forever.
        allowed = Allowlist(["octocat/dormant"])
        with _Patched(tt, discover_repos=lambda: ["someone/busy"]):
            self.assertEqual(tt.candidate_repos(allowed), ["octocat/dormant"])

    def test_an_empty_but_readable_list_permits_nothing(self):
        # Read the list and it grants nothing. Falling back to discovery here
        # would hand back exactly the repositories the owner did not grant.
        with _Patched(tt, discover_repos=lambda: ["someone/busy"]):
            self.assertEqual(tt.candidate_repos(Allowlist([])), [])

    def test_an_unreadable_list_falls_back_to_discovery(self):
        # Those candidates are then denied one by one in main(), which is the
        # point: the tick reports WHY it did nothing instead of looking idle.
        with _Patched(tt, discover_repos=lambda: ["someone/busy"]):
            self.assertEqual(tt.candidate_repos(Allowlist.denied()),
                             ["someone/busy"])

    def test_the_cap_is_announced_rather_than_silent(self):
        allowed = Allowlist([f"o/r{i}" for i in range(5)])
        err = io.StringIO()
        with _Patched(tt, MAX_REPOS=2), contextlib.redirect_stderr(err):
            got = tt.candidate_repos(allowed)
        self.assertEqual(got, ["o/r0", "o/r1"])
        self.assertIn("o/r4", err.getvalue(),
                      "a silently dropped repository reads as 'tested, clean'")


class PermissionAnswers(unittest.TestCase):
    """`project-allow check`'s exit code, and what each one means."""

    def test_zero_is_permitted(self):
        self.assertEqual(tt.permission_reason(0), "")

    def test_two_is_not_permitted(self):
        self.assertEqual(tt.permission_reason(2), "not-permitted")

    def test_no_answer_permits_nothing(self):
        # The CLI missing, the exec failing, the list unreadable: all of them
        # mean we could not ask, and not asking is not permission.
        self.assertEqual(tt.permission_reason(None), "allowlist-unavailable")
        self.assertEqual(tt.permission_reason(1), "allowlist-unavailable")


class Planning(unittest.TestCase):
    """main(), with the pod and GitHub replaced."""

    def plan(self, *, allowed, queues=None, perms=None, locks=(), heads=None,
             head=("main", "sha-new")):
        fetched = []

        def head_sha(full_name):
            fetched.append(full_name)
            return head

        with _Patched(
            tt,
            k8s_namespace=lambda: "claw-code-local",
            find_openclaw_pod=lambda ns: "openclaw-1",
            query_pod_state=lambda ns, pod: (
                set(locks), dict(heads or {}), allowed, dict(queues or {})),
            query_permissions=lambda ns, pod, repos: (
                {r: (perms or {}).get(r, 0) for r in repos}, True),
            head_sha_for_default_branch=head_sha,
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                tt.main()
        return json.loads(out.getvalue()), fetched

    # -- the queue hold-back --------------------------------------------

    def test_a_non_empty_solver_queue_holds_the_tick_back(self):
        plan, fetched = self.plan(
            allowed=Allowlist(["o/r"]), queues={"solver": fresh(3)})
        self.assertEqual(plan["repos"], [])
        self.assertIn("solve and merge first", plan["skipped"])
        self.assertEqual(plan["queues"], {"solver": 3})
        self.assertEqual(fetched, [],
                         "held back, so it must not spend API calls either")

    def test_a_non_empty_reviewer_queue_holds_the_tick_back(self):
        plan, _ = self.plan(
            allowed=Allowlist(["o/r"]), queues={"reviewer": fresh(1)})
        self.assertIn("1 reviewer", plan["skipped"])

    def test_the_reason_names_every_busy_queue(self):
        # The spawner prints this verbatim; a reason that names one of two
        # queues sends the reader looking at the wrong subsystem.
        plan, _ = self.plan(allowed=Allowlist(["o/r"]),
                            queues={"solver": fresh(2), "reviewer": fresh(4)})
        self.assertIn("2 solver", plan["skipped"])
        self.assertIn("4 reviewer", plan["skipped"])

    def test_empty_queues_do_not_hold_the_tick_back(self):
        plan, _ = self.plan(allowed=Allowlist(["o/r"]),
                            queues={"solver": fresh(0), "reviewer": fresh(0)})
        self.assertNotIn("skipped", plan)
        self.assertTrue(plan["repos"][0]["toSpawn"])

    def test_a_STALE_marker_FAILS_OPEN(self):
        # The whole point. A planner that crashed or was suspended leaves its
        # last marker behind saying "3 pending" forever. Treating that as a
        # block stops every deployment from ever being tested again, with
        # nothing in the logs to say why — far worse than an occasional
        # overlap, which costs only tokens.
        stale = fresh(3, age=queue_state.DEFAULT_TTL_SECONDS + 60)
        plan, fetched = self.plan(allowed=Allowlist(["o/r"]),
                                  queues={"solver": stale})
        self.assertNotIn("skipped", plan)
        self.assertEqual(fetched, ["o/r"])
        self.assertTrue(plan["repos"][0]["toSpawn"])

    def test_no_marker_at_all_fails_open_too(self):
        # A deployment where the planners have never published anything must
        # still test. Same reasoning as the stale case: unknown is not blocked.
        plan, _ = self.plan(allowed=Allowlist(["o/r"]), queues={})
        self.assertNotIn("skipped", plan)
        self.assertTrue(plan["repos"][0]["toSpawn"])

    def test_a_stale_marker_does_not_mask_a_fresh_busy_one(self):
        plan, _ = self.plan(allowed=Allowlist(["o/r"]), queues={
            "solver": fresh(3, age=queue_state.DEFAULT_TTL_SECONDS + 60),
            "reviewer": fresh(2)})
        self.assertIn("2 reviewer", plan["skipped"])
        self.assertNotIn("3 solver", plan["skipped"])

    # -- the allowlist gate ---------------------------------------------

    def test_a_permitted_repository_is_planned(self):
        plan, _ = self.plan(allowed=Allowlist(["o/r"]))
        entry = plan["repos"][0]
        self.assertEqual((entry["repo"], entry["toSpawn"], entry["headSha"]),
                         ("o/r", True, "sha-new"))
        self.assertTrue(plan["allowlistAvailable"])
        self.assertEqual(plan["allowedProjects"], 1)

    def test_project_allow_exit_2_denies_the_repository(self):
        plan, fetched = self.plan(allowed=Allowlist(["o/r", "o/other"]),
                                  perms={"o/other": 2})
        by_repo = {r["repo"]: r for r in plan["repos"]}
        self.assertEqual(by_repo["o/other"]["reason"], "not-permitted")
        self.assertFalse(by_repo["o/other"]["toSpawn"])
        self.assertTrue(by_repo["o/r"]["toSpawn"])
        # Permission is decided BEFORE the per-repository API calls: nothing
        # about a repository the bot may not touch is worth a request.
        self.assertEqual(fetched, ["o/r"])

    def test_an_unanswerable_permission_check_denies_the_repository(self):
        plan, fetched = self.plan(allowed=Allowlist(["o/r"]), perms={"o/r": 1})
        self.assertEqual(plan["repos"][0]["reason"], "allowlist-unavailable")
        self.assertEqual(fetched, [])

    def test_an_unreadable_list_reports_itself_as_unreadable(self):
        # cron-tester-spawn keys its "could not read the list" warning off
        # this flag, so it is a contract and not decoration.
        def boom():
            raise RuntimeError("GitHub 401")

        with _Patched(tt, discover_repos=boom):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                plan, _ = self.plan(allowed=Allowlist.denied())
        self.assertIs(plan["allowlistAvailable"], False)
        self.assertIsNone(plan["allowedProjects"])
        self.assertEqual(plan["repos"], [])
        # A failed fallback must not become a generic "planner error": the
        # list is the thing to fix, and that is what the operator is told.
        self.assertNotIn("error", plan)

    # -- the ordinary skips ---------------------------------------------

    def test_an_unchanged_head_is_not_retested(self):
        plan, _ = self.plan(allowed=Allowlist(["o/r"]),
                            heads={"o__r": "sha-new"})
        self.assertEqual(plan["repos"][0]["reason"], "head-unchanged")
        self.assertFalse(plan["repos"][0]["toSpawn"])

    def test_a_live_lock_blocks_a_second_runner(self):
        plan, fetched = self.plan(allowed=Allowlist(["o/r"]), locks={"o__r"})
        self.assertEqual(plan["repos"][0]["reason"], "lock-held")
        self.assertEqual(fetched, [])

    def test_a_head_that_cannot_be_fetched_is_skipped_not_guessed(self):
        plan, _ = self.plan(allowed=Allowlist(["o/r"]), head=("main", ""))
        self.assertEqual(plan["repos"][0]["reason"], "head-fetch-failed")


if __name__ == "__main__":
    unittest.main()

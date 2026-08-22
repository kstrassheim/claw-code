"""The document shapes — the part of the store that is expensive to change.

Documents outlive the code that wrote them. A key scheme that turns out to
collide, or a field that quietly means two things, is not a bug you fix in a
deploy: it is a collection full of data that has to be migrated or thrown away.
So the shapes get tests of their own, and each one names the collision or the
ambiguity it rules out.
"""

import unittest

from harness import load


class KeysAreHostAware(unittest.TestCase):
    def setUp(self):
        self.d = load("planning_docs")

    def test_a_story_key_carries_host_repo_and_number(self):
        self.assertEqual(self.d.story_pk("github", "acme/web", 42),
                         "story#github#acme/web#42")

    def test_two_hosts_do_not_collide_on_the_same_repo_path(self):
        # `owner/repo` is spelled identically on github.com and on a
        # self-hosted GitHub Enterprise instance. Without the host segment
        # these are one key, and the day a second host is configured every
        # report silently merges two repositories.
        a = self.d.story_pk("github", "acme/web", 42)
        b = self.d.story_pk("ghe-internal", "acme/web", 42)
        self.assertNotEqual(a, b)

    def test_a_key_is_normalised_the_same_way_every_time(self):
        # Keys are built through story_pk on the write path AND on every
        # query that looks for them. A caller that formats the string by hand
        # drifts the moment a repo name contains something _clean rewrites,
        # and the query then matches nothing while looking correct.
        weird = self.d.story_pk("github", "acme/web (old)", 42)
        self.assertNotIn(" ", weird)
        self.assertEqual(weird, self.d.story_pk("github", "acme/web (old)", 42))

    def test_a_number_given_as_a_string_or_an_int_is_one_key(self):
        # The runners pass whatever the API handed them. Two spellings of the
        # same issue would be two stories.
        self.assertEqual(self.d.story_pk("github", "acme/web", 42),
                         self.d.story_pk("github", "acme/web", "42"))

    def test_a_deploy_key_is_per_tested_commit(self):
        pk = self.d.deploy_pk("github", "acme/web", "abcdef1234567890")
        self.assertEqual(pk, "deploy#github#acme/web#abcdef123456")

    def test_the_only_configured_host_is_github(self):
        self.assertEqual(self.d.HOSTS, ("github",))

    def test_a_sprint_key_is_just_its_number(self):
        # Sprints are global, so no host: sprint 4 is sprint 4 regardless of
        # which repositories its stories came from.
        self.assertEqual(self.d.sprint_pk(4), "sprint#4")

    def test_a_worker_key_is_host_aware_like_the_others(self):
        # User ids are only unique within one host.
        self.assertEqual(self.d.worker_pk("github", 1234), "worker#github#1234")

    def test_the_four_key_kinds_cannot_be_confused_for_each_other(self):
        keys = {self.d.story_pk("github", "a/b", 1),
                self.d.deploy_pk("github", "a/b", "abc123"),
                self.d.sprint_pk(1),
                self.d.worker_pk("github", 1)}
        self.assertEqual(len(keys), 4)
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(key.split("#", 1)[0],
                              (self.d.STORY, self.d.DEPLOY, self.d.SPRINT,
                               self.d.WORKER))


class StoriesSpeakOfPullRequests(unittest.TestCase):
    def setUp(self):
        self.d = load("planning_docs")
        self.story = self.d.story_doc(host="github", repo="acme/web", number=42,
                                      title="Fix the thing", now="2026-08-17")

    def test_the_pull_request_fields_are_named_for_what_they_are(self):
        self.assertIn("prOpenedAt", self.story)
        self.assertIn("prUrl", self.story)

    def test_the_id_and_the_key_are_the_same_string(self):
        # A story is one document per key, so anything else would be a second
        # story for the same issue.
        self.assertEqual(self.story["id"], self.story["pk"])

    def test_a_fresh_story_has_no_timestamps_rather_than_empty_ones(self):
        # Absent is meaningful: it has not happened yet, which is different
        # from happening at an unknown time.
        for key in ("startedAt", "prOpenedAt", "mergedAt"):
            with self.subTest(key=key):
                self.assertIsNone(self.story[key])

    def test_a_deploy_records_the_pull_requests_it_contained(self):
        dep = self.d.deploy_doc(host="github", repo="acme/web", sha="abc123def456",
                                pull_requests=[7, 9], covered_stories=["x"])
        self.assertEqual(dep["pullRequests"], [7, 9])

    def test_an_estimate_carries_whether_it_was_judged(self):
        # A defaulted 8 and a judged 8 are not the same fact, and from the
        # number alone nobody can tell them apart six weeks later.
        judged = self.d.story_doc(host="github", repo="a/b", number=1, title="t",
                                  story_points=8)
        guessed = self.d.story_doc(host="github", repo="a/b", number=2, title="t",
                                   story_points=8, points_defaulted=True)
        self.assertFalse(judged["pointsDefaulted"])
        self.assertTrue(guessed["pointsDefaulted"])


class WorkEventsAreAppendOnlyAndIdempotent(unittest.TestCase):
    def setUp(self):
        self.d = load("planning_docs")

    def work(self, run_id="run-1", role="solver", calls=0):
        return self.d.work_doc(host="github", repo="acme/web", number=42,
                               run_id=run_id, role=role, llm_calls=calls)

    def test_the_id_is_derived_from_the_run_and_the_role(self):
        # This is what makes recording on a timer safe: every write during a
        # long solve replaces the previous partial instead of adding a row.
        self.assertEqual(self.work()["id"], "work#run-1#solver")
        self.assertEqual(self.work(calls=137)["id"], self.work()["id"])

    def test_the_solver_and_the_reviewer_do_not_overwrite_each_other(self):
        self.assertNotEqual(self.work(role="solver")["id"],
                            self.work(role="reviewer")["id"])

    def test_a_work_event_lives_under_the_story_it_was_work_on(self):
        # Shared key, so "the story and everything that happened to it" is one
        # equality match rather than an $or across shapes.
        self.assertEqual(self.work()["pk"],
                         self.d.story_pk("github", "acme/web", 42))

    def test_a_work_event_is_kept_unless_it_is_given_an_expiry(self):
        # A TTL index acts on documents that HAVE the field; everything else
        # is kept. Emitting an expiry by default would quietly delete history.
        self.assertNotIn("expiresAt", self.work())
        self.assertEqual(
            self.d.work_doc(host="github", repo="a/b", number=1, run_id="r",
                            role="solver",
                            expires_at="2026-12-01T00:00:00+00:00")["expiresAt"],
            "2026-12-01T00:00:00+00:00")

    def test_stories_sprints_and_workers_never_expire(self):
        self.assertNotIn("expiresAt", self.d.story_doc(
            host="github", repo="a/b", number=1, title="t"))
        self.assertNotIn("expiresAt", self.d.sprint_doc(
            number=1, started_at="2026-08-01"))
        self.assertNotIn("expiresAt", self.d.worker_doc(
            host="github", user_id=1))


class TheTesterHasNoStory(unittest.TestCase):
    """The deploy work unit, which is the shape that is easiest to get wrong."""

    def setUp(self):
        self.d = load("planning_docs")

    def test_a_tester_run_lives_under_the_commit_it_tested(self):
        # The tester keys on (repo, HEAD sha) and tests a RANGE. Forcing it
        # into a story key would put a document in the store describing work
        # on an issue that does not exist.
        doc = self.d.tester_work_doc(host="github", repo="acme/web",
                                     sha="abc123def456", run_id="t1")
        self.assertEqual(doc["pk"],
                         self.d.deploy_pk("github", "acme/web", "abc123def456"))
        self.assertEqual(doc["role"], "tester")

    def test_a_tester_run_in_a_story_partition_is_rejected(self):
        doc = self.d.tester_work_doc(host="github", repo="acme/web",
                                     sha="abc123def456", run_id="t1")
        doc["pk"] = self.d.story_pk("github", "acme/web", 42)
        problems = self.d.validate(doc)
        self.assertTrue(any("deploy" in p for p in problems))

    def test_a_deploy_may_legitimately_cover_no_stories(self):
        # A commit that reached the default branch outside the bot's flow — a
        # human push, a revert. Empty is a fact worth being able to see, not a
        # failure to resolve them.
        dep = self.d.deploy_doc(host="github", repo="acme/web", sha="abc123")
        self.assertEqual(dep["coveredStories"], [])
        self.assertEqual(self.d.validate(dep), [])

    def test_the_tester_shares_the_work_shape(self):
        # So a sprint can sum story work and deploy work without special
        # casing; only the key differs.
        story_work = self.d.work_doc(host="github", repo="a/b", number=1,
                                     run_id="r", role="solver")
        deploy_work = self.d.tester_work_doc(host="github", repo="a/b",
                                             sha="abc123", run_id="r")
        self.assertEqual(set(story_work) - {"expiresAt"},
                         set(deploy_work) - {"expiresAt"})


class HowAStoryEnteredItsSprint(unittest.TestCase):
    def setUp(self):
        self.d = load("planning_docs")

    def test_arriving_after_the_start_is_added_not_committed(self):
        # Counting scope growth as commitment erases the one difference the
        # sprint report exists to show.
        self.assertEqual(
            self.d.derive_scope(sprint_started_at="2026-08-01T13:00:00+00:00",
                                entered_at="2026-08-04T09:00:00+00:00"),
            "added")

    def test_arriving_at_the_start_is_committed(self):
        self.assertEqual(
            self.d.derive_scope(sprint_started_at="2026-08-01T13:00:00+00:00",
                                entered_at="2026-08-01T13:00:00+00:00"),
            "committed")

    def test_a_story_from_another_sprint_is_carried_whenever_it_arrives(self):
        # A carried story is not a planning miss in the sprint inheriting it.
        self.assertEqual(
            self.d.derive_scope(sprint_started_at="2026-08-01T13:00:00+00:00",
                                entered_at="2026-08-04T09:00:00+00:00",
                                came_from_sprint=3),
            "carried")

    def test_a_missing_timestamp_is_unknown_and_not_a_guess(self):
        # Guessing "committed" inflates every commitment figure it appears in.
        self.assertEqual(
            self.d.derive_scope(sprint_started_at="", entered_at="x"),
            "unknown")

    def test_entering_a_sprint_appends_to_the_history(self):
        story = self.d.story_doc(host="github", repo="a/b", number=1, title="t")
        self.d.enter_sprint(story, sprint_id=1, scope="committed", at="t1")
        self.d.enter_sprint(story, sprint_id=2, scope="carried", at="t2")
        self.assertEqual([h["sprintId"] for h in story["sprintHistory"]], [1, 2])
        self.assertEqual(story["sprintScope"], "carried")

    def test_an_unrecognised_scope_becomes_unknown_rather_than_being_stored(self):
        story = self.d.story_doc(host="github", repo="a/b", number=1, title="t")
        self.d.enter_sprint(story, sprint_id=1, scope="somehow", at="t1")
        self.assertEqual(story["sprintScope"], "unknown")


class ValidationCatchesWhatWouldSkewAReport(unittest.TestCase):
    def setUp(self):
        self.d = load("planning_docs")

    def test_a_well_formed_story_has_no_problems(self):
        self.assertEqual(self.d.validate(self.d.story_doc(
            host="github", repo="a/b", number=1, title="t")), [])

    def test_an_unknown_role_is_a_problem(self):
        doc = self.d.work_doc(host="github", repo="a/b", number=1,
                              run_id="r", role="intern")
        self.assertTrue(any("role" in p for p in self.d.validate(doc)))

    def test_a_work_event_outside_any_known_key_is_a_problem(self):
        # Anywhere else and the sprint arithmetic silently misses it.
        doc = self.d.work_doc(host="github", repo="a/b", number=1,
                              run_id="r", role="solver")
        doc["pk"] = "somewhere#else"
        self.assertTrue(any("story or deploy" in p for p in self.d.validate(doc)))

    def test_a_story_in_a_sprint_with_no_scope_is_flagged(self):
        # It counts as neither committed, added nor carried, so it vanishes
        # from the arithmetic with nothing saying why.
        story = self.d.story_doc(host="github", repo="a/b", number=1, title="t",
                                 sprint_id=4)
        self.assertTrue(any("sprintScope" in p or "scope" in p
                            for p in self.d.validate(story)))

    def test_negative_model_calls_are_rejected(self):
        doc = self.d.work_doc(host="github", repo="a/b", number=1,
                              run_id="r", role="solver")
        doc["llmCalls"] = -5
        self.assertIn("llmCalls is negative", self.d.validate(doc))


if __name__ == "__main__":
    unittest.main()

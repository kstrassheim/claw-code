"""The `planning` command, which is read by an agent as often as by a person.

`schema` is the one output here with a hard correctness requirement: the agent
composes its own queries from it. A schema that describes fields the documents
do not have produces queries that return nothing, and an empty result is
indistinguishable from "there is no such data" — so the agent draws a confident
wrong conclusion and nothing anywhere reports an error.

Everything else in this file pins the same property from the other side: every
report degrades into a sentence rather than a traceback, because "the store is
not provisioned yet" is a NORMAL state of this system and not a fault.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest

from harness import TMP_ROOT, load, load_script, temp_env


class CliTestCase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="cli-", dir=TMP_ROOT)
        env = temp_env(PLANNING_MONGO_URI=None,
                       PLANNING_SPOOL=os.path.join(self.dir, "spool.jsonl"))
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        load("planning_store")
        self.cli = load_script("planning")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.cli.main(list(argv))
        return rc, out.getvalue()


class TheSchemaDescribesTheRealDocuments(CliTestCase):
    def test_the_key_scheme_is_spelled_out(self):
        rc, text = self.run_cli("schema")
        self.assertEqual(rc, 0)
        self.assertIn("story#<host>#<owner/repo>#<issue-number>", text)
        self.assertIn("deploy#<host>#<owner/repo>#<sha12>", text)
        self.assertIn("sprint#<n>", text)

    def test_the_id_scheme_and_the_upsert_are_stated(self):
        # It is why a runner may record the same run every two minutes. An
        # agent that does not know this reads a re-record as a second run.
        _, text = self.run_cli("schema")
        self.assertIn("_id", text)
        self.assertIn("work#<runId>#<role>", text)
        self.assertIn("upsert", text)

    def test_the_named_fields_are_fields_the_documents_actually_have(self):
        # The check that keeps the schema honest as the shapes change: every
        # field named below is pulled from a real document, not from prose.
        docs = load("planning_docs")
        _, text = self.run_cli("schema")
        story = docs.story_doc(host="github", repo="a/b", number=1, title="t")
        work = docs.work_doc(host="github", repo="a/b", number=1, run_id="r",
                             role="solver")
        deploy = docs.deploy_doc(host="github", repo="a/b", sha="abc123")
        for field in ("storyPoints", "pointsDefaulted", "sprintScope",
                      "prOpenedAt", "prUrl", "sprintHistory"):
            with self.subTest(field=field):
                self.assertIn(field, story)
                self.assertIn(field, text)
        for field in ("llmCalls", "secondsOn429", "runId"):
            with self.subTest(field=field):
                self.assertIn(field, work)
                self.assertIn(field, text)
        for field in ("coveredStories", "pullRequests", "priorSha"):
            with self.subTest(field=field):
                self.assertIn(field, deploy)
                self.assertIn(field, text)

    def test_the_scope_and_origin_values_come_from_the_module(self):
        # Generated rather than written out, so a new value cannot be added
        # without the schema learning about it.
        docs = load("planning_docs")
        _, text = self.run_cli("schema")
        for value in docs.SCOPES + docs.ORIGINS:
            with self.subTest(value=value):
                self.assertIn(value, text)

    def test_it_warns_that_work_documents_have_no_repo_field(self):
        # The single most likely wrong query: matching work by repo returns
        # nothing, forever, silently.
        _, text = self.run_cli("schema")
        self.assertIn("no repo or number of its own", text)

    def test_the_examples_are_valid_json(self):
        import json
        import re
        _, text = self.run_cli("schema")
        found = re.findall(r"planning query '(\{.*?\})'", text)
        self.assertTrue(found, "the schema must show a usable example")
        for example in found:
            with self.subTest(example=example):
                json.loads(example)


class NothingRaisesWhenTheStoreIsAbsent(CliTestCase):
    def test_status_says_it_is_not_provisioned_rather_than_failing(self):
        rc, text = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("NOT PROVISIONED YET", text)

    def test_status_never_claims_a_configured_store_is_reachable(self):
        rc, text = self.run_cli("status")
        self.assertNotIn("reachable  : yes", text)

    def test_every_report_degrades_to_a_sentence(self):
        for argv in (["sprint"], ["velocity"], ["deploys"], ["rework"],
                     ["forecast"], ["burndown"], ["burnup"], ["chart"],
                     ["story", "acme/web#1"]):
            with self.subTest(argv=argv):
                rc, text = self.run_cli(*argv)
                self.assertEqual(rc, 0)
                self.assertIn("not reachable", text)

    def test_the_history_report_stays_silent(self):
        # Its output is pasted into an estimation prompt. "The store is
        # unreachable" would be read by the model as a statement about the
        # story it is sizing.
        rc, text = self.run_cli("history", "acme/web")
        self.assertEqual(rc, 0)
        self.assertEqual(text, "")

    def test_a_story_reference_must_name_the_issue(self):
        rc, text = self.run_cli("story", "acme/web")
        self.assertEqual(rc, 1)
        self.assertIn("owner/repo", text)


class RawQueries(CliTestCase):
    def test_a_broken_filter_says_what_was_wrong(self):
        # A silent empty result here reads exactly like "there is no such
        # data", which is the one wrong lesson to draw from a typo.
        rc, text = self.run_cli("query", "{type: story}")
        self.assertEqual(rc, 2)
        self.assertIn("not valid JSON", text)

    def test_a_pipeline_must_be_an_array_of_stages(self):
        rc, text = self.run_cli("query", '{"type": "story"}', "--pipeline")
        self.assertEqual(rc, 2)
        self.assertIn("array", text)

    def test_a_filter_must_be_an_object(self):
        rc, text = self.run_cli("query", '[{"$match": {}}]')
        self.assertEqual(rc, 2)
        self.assertIn("--pipeline", text)


class DeferringToTheNextSprint(CliTestCase):
    def test_the_spellings_people_and_models_write(self):
        for form in ("next sprint", "Next Sprint", "nextsprint", "next-sprint",
                     "next_sprint", "Sprint::Next Sprint"):
            with self.subTest(form=form):
                self.assertTrue(self.cli.next_sprint_labels([form]))

    def test_an_unrelated_label_is_left_alone(self):
        for form in ("sprint", "next", "next release", "Priority::High", "bug"):
            with self.subTest(form=form):
                self.assertEqual(self.cli.next_sprint_labels([form]), [])

    def test_the_exact_spelling_is_preserved_for_removal(self):
        # GitHub deletes a label by its literal name: sending "nextsprint"
        # would not remove one written "Next Sprint", and the deferral would
        # quietly become permanent — "later" turning into "never" with the
        # issue sitting assigned and untouched.
        self.assertEqual(
            self.cli.next_sprint_labels(["Next Sprint", "bug"]),
            ["Next Sprint"])

    def test_labels_arriving_as_api_objects_are_understood(self):
        # The issues endpoint returns label objects, not strings.
        self.assertEqual(
            self.cli.next_sprint_labels([{"name": "next-sprint"},
                                         {"name": "bug"}]),
            ["next-sprint"])

    def test_clearing_does_nothing_without_a_token(self):
        # A rollover must never depend on GitHub being reachable.
        with temp_env(GITHUB_TOKEN=None):
            self.assertEqual(self.cli.clear_next_sprint_labels(), 0)


class TheRolloverTick(unittest.TestCase):
    """`planning sprint-tick`, which is what makes sprints autonomous."""

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="tick-", dir=TMP_ROOT)
        self.marker = os.path.join(self.dir, "sprint-current.json")
        self.spool = os.path.join(self.dir, "spool.jsonl")
        env = temp_env(PLANNING_MONGO_URI=None,
                       PLANNING_SPOOL=self.spool,
                       GITHUB_TOKEN=None,
                       SPRINT_CURRENT_FILE=self.marker,
                       SPRINT_SCHEDULE_CONF=os.path.join(self.dir, "sched.conf"))
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        load("planning_store")
        load("sprint_current")
        load("sprint_schedule")
        self.cli = load_script("planning")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def tick(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.cli.main(["sprint-tick"])
        return rc, out.getvalue()

    def spooled(self):
        import json
        if not os.path.exists(self.spool):
            return []
        with open(self.spool, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_the_first_tick_opens_sprint_one_without_being_asked(self):
        rc, text = self.tick()
        self.assertEqual(rc, 0)
        self.assertIn("sprint 1 started", text)
        self.assertEqual(load("sprint_current").number(), 1)

    def test_the_sprint_document_is_written_even_with_no_store(self):
        # Guarding this behind enabled() would mean the document is never
        # created at all while the output promises it has been kept: the
        # sprint would exist in the local marker, every runner would log work
        # against it, and no sprint document would exist for any report to
        # attach that work to.
        self.tick()
        sprints = [d for d in self.spooled() if d.get("type") == "sprint"]
        self.assertEqual(len(sprints), 1)
        self.assertEqual(sprints[0]["number"], 1)
        self.assertEqual(sprints[0]["state"], "active")

    def test_ticking_again_does_nothing(self):
        # It runs every few minutes. A hundred ticks in a row must not open a
        # hundred sprints.
        self.tick()
        rc, text = self.tick()
        self.assertEqual(rc, 0)
        self.assertNotIn("started", text)
        self.assertEqual(load("sprint_current").number(), 1)
        self.assertEqual(
            len([d for d in self.spooled() if d.get("type") == "sprint"]), 1)

    def test_the_marker_is_written_before_anything_that_can_fail(self):
        # It is what every runner reads, and it cannot fail on a network. A
        # sprint that exists locally but not yet in the store is far better
        # than the reverse, where work events carry a number nothing resolves.
        self.tick()
        self.assertTrue(os.path.exists(self.marker))

    def test_the_schedule_is_reported_in_words(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.cli.main(["schedule"])
        self.assertEqual(rc, 0)
        self.assertIn("Automatic sprints", out.getvalue())

    def test_a_schedule_it_cannot_read_is_refused(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.cli.main(["schedule", "sometime"])
        self.assertEqual(rc, 2)
        self.assertIn("could not find", out.getvalue())


class ResolvingWhichSprintIsMeant(CliTestCase):
    def test_a_missing_sprint_is_a_sentence_and_not_an_exception(self):
        # `current` and `last` are how sprints are named in chat far more
        # often than by number; int("current") would raise, and to a chat
        # agent a traceback reads as "the reporting tool is broken".
        for which in (None, "current", "last", "4", "nonsense"):
            with self.subTest(which=which):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertIsNone(self.cli.resolve_sprint(which))
                self.assertTrue(out.getvalue().strip())

    def test_next_sprint_is_answered_without_inventing_a_document(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(self.cli.resolve_sprint("next"))
        self.assertIn("has not started yet", out.getvalue())

    def test_the_stories_of_a_sprint_are_asked_for_by_sprint_id(self):
        self.assertEqual(self.cli._stories_in_sprint(4), [])


class SprintDateArithmetic(CliTestCase):
    def test_the_days_of_a_sprint_are_inclusive_of_both_ends(self):
        days = self.cli._days("2026-08-08T13:00:00+00:00",
                              "2026-08-11T13:00:00+00:00")
        self.assertEqual(days,
                         ["2026-08-08", "2026-08-09", "2026-08-10", "2026-08-11"])

    def test_unusable_dates_draw_nothing_rather_than_raising(self):
        self.assertEqual(self.cli._days("", ""), [])
        self.assertEqual(self.cli._days("2026-08-11", "2026-08-08"), [])

    def test_a_burndown_counts_scope_from_the_day_it_arrived(self):
        # A line that only ever falls is one that is hiding scope growth; the
        # flat stretches are the arrivals, and they are the point of the chart.
        sprint = {"startedAt": "2026-08-08", "endsAt": "2026-08-10"}
        stories = [
            {"storyPoints": 3, "enteredSprintAt": "2026-08-08"},
            {"storyPoints": 5, "enteredSprintAt": "2026-08-10"},
        ]
        _, remaining, _ = self.cli._burndown_series(sprint, stories)
        self.assertEqual(remaining, [3, 3, 8])

    def test_a_merged_story_leaves_the_remaining_line_that_day(self):
        sprint = {"startedAt": "2026-08-08", "endsAt": "2026-08-10"}
        stories = [{"storyPoints": 3, "enteredSprintAt": "2026-08-08",
                    "mergedAt": "2026-08-09T11:00:00+00:00"}]
        _, remaining, _ = self.cli._burndown_series(sprint, stories)
        self.assertEqual(remaining, [3, 0, 0])


if __name__ == "__main__":
    unittest.main()

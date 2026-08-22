"""Writing the story document — the record everything else in planning reads.

WHY THIS FILE IS HERE AT ALL

The planning store holds three kinds of document: the SPRINT, the STORY, and
the WORK events recorded against a story. Two of them were being written and
the story was not, by anything. Nothing errored. `planning status` reported
the store reachable, documents accumulated, and every sprint report read
`0 pts (0 stories)` in every bucket — for work that had been estimated,
implemented, merged and closed. The delivery sweep showed the same hole from
the other side: it found the merges and had no story to attach them to.

So the first test here is the one that would have failed: writing a story
produces a document of type `story`. The rest defend the two properties that
make writing it from more than one place safe.

THE WRITE IS A MERGE, NOT A REPLACE. Estimation knows the size and the labels,
the solver knows the title and the URL, the delivery sweep knows the merge. If
the last writer won, each caller would blank what it does not know, and a
delivery date already read in a report would quietly disappear.

SPRINT MEMBERSHIP IS DECIDED ONCE. A story that is already in the running
sprint keeps the scope it entered with — re-estimating something mid-sprint
must not silently reclassify committed work as added, because that is the
number the sprint is judged against.
"""

import json
import os
import re
import shutil
import tempfile
import unittest

from harness import BUILDER, TMP_ROOT, load, load_script, temp_env


class StoryWrites(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="story-", dir=TMP_ROOT)
        self.spool = os.path.join(self.dir, "planning-spool.jsonl")
        self.sprint_file = os.path.join(self.dir, "sprint.json")
        env = temp_env(PLANNING_SPOOL=self.spool,
                       PLANNING_MONGO_URI=None,
                       SPRINT_CURRENT_FILE=self.sprint_file)
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        load("planning_store")
        load("sprint_current")
        self.cmd = load_script("planning-story")
        # No store is reachable in the suite, so every read is empty unless a
        # test says otherwise. Tests that need a prior document install one.
        self.existing = []
        self.cmd.planning_store.query = lambda *a, **k: list(self.existing)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def set_sprint(self, number, started_at):
        with open(self.sprint_file, "w", encoding="utf-8") as f:
            json.dump({"number": number, "startedAt": started_at,
                       "endsAt": "2099-01-01T00:00:00+0000"}, f)
        load("sprint_current")
        self.cmd = load_script("planning-story")
        self.cmd.planning_store.query = lambda *a, **k: list(self.existing)

    def spooled(self):
        if not os.path.exists(self.spool):
            return []
        with open(self.spool, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def run_story(self, *argv):
        return self.cmd.main(list(argv))

    # --- the document exists at all -----------------------------------------

    def test_estimating_a_story_writes_a_story_document(self):
        # The regression this whole file exists for: before this, nothing
        # anywhere created one, and every report read zero without an error.
        rc = self.run_story("--repo", "acme/web", "--issue", "42",
                            "--title", "Add login", "--points", "5",
                            "--estimator", "planning")
        self.assertEqual(rc, 0)
        docs = self.spooled()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["type"], "story")
        self.assertEqual(docs[0]["pk"], "story#github#acme/web#42")
        self.assertEqual(docs[0]["storyPoints"], 5)
        self.assertEqual(docs[0]["title"], "Add login")

    def test_recording_the_same_story_twice_keeps_one_id(self):
        # Deterministic id, upsert write. A re-estimate must update the story,
        # not add a second one the reports would then count twice.
        for _ in range(3):
            self.run_story("--repo", "acme/web", "--issue", "42",
                           "--points", "5")
        self.assertEqual({d["id"] for d in self.spooled()},
                         {"story#github#acme/web#42"})

    def test_an_unsized_story_is_recorded_without_a_size(self):
        # The solver records the story it is working even when no size
        # travelled with it. A story with no number is still a story, and it
        # is what the delivery sweep needs in order to attach the merge.
        self.assertEqual(
            self.run_story("--repo", "acme/web", "--issue", "42"), 0)
        doc = self.spooled()[0]
        self.assertIsNone(doc["storyPoints"])
        self.assertEqual(doc["estimatedAt"], "")

    def test_a_size_that_is_not_a_number_is_recorded_as_unsized(self):
        # Never guess. An unparseable size is absent, not zero — zero is a
        # legitimate value on some scales and would be counted as delivered.
        self.run_story("--repo", "acme/web", "--issue", "42",
                       "--points", "large")
        self.assertIsNone(self.spooled()[0]["storyPoints"])

    def test_labels_arrive_as_a_list(self):
        self.run_story("--repo", "acme/web", "--issue", "42",
                       "--labels", "bug, SP::5 ,")
        self.assertEqual(self.spooled()[0]["labels"], ["bug", "SP::5"])

    # --- the merge -----------------------------------------------------------

    def test_a_later_write_does_not_blank_the_delivery_date(self):
        # The sweep fills mergedAt. If a re-estimate replaced the document,
        # the story would leave the completed column of a report that has
        # already been read, with nothing logged anywhere.
        self.existing = [{
            "id": "story#github#acme/web#42", "type": "story",
            "mergedAt": "2026-08-01T10:00:00+0000",
            "deliveredBy": "cameron-claw",
            "prUrl": "https://github.com/acme/web/pull/7",
        }]
        self.run_story("--repo", "acme/web", "--issue", "42", "--points", "5")
        doc = self.spooled()[0]
        self.assertEqual(doc["mergedAt"], "2026-08-01T10:00:00+0000")
        self.assertEqual(doc["deliveredBy"], "cameron-claw")
        self.assertEqual(doc["prUrl"], "https://github.com/acme/web/pull/7")

    def test_the_solver_does_not_blank_what_estimation_knew(self):
        # The solver knows the title and the URL and not the labels or the
        # size. Recording from there must fill, never empty.
        self.existing = [{
            "id": "story#github#acme/web#42", "type": "story",
            "storyPoints": 5, "estimator": "planning",
            "estimatedAt": "2026-08-01T09:00:00+0000",
            "labels": ["bug"], "priority": "P2",
        }]
        self.run_story("--repo", "acme/web", "--issue", "42",
                       "--title", "Add login",
                       "--url", "https://github.com/acme/web/issues/42")
        doc = self.spooled()[0]
        self.assertEqual(doc["storyPoints"], 5)
        self.assertEqual(doc["labels"], ["bug"])
        self.assertEqual(doc["priority"], "P2")
        self.assertEqual(doc["estimatedAt"], "2026-08-01T09:00:00+0000")
        self.assertEqual(doc["title"], "Add login")

    def test_a_new_value_wins_over_the_stored_one(self):
        # Merging fills gaps; it does not freeze the document. A re-estimate
        # that produces a different number has to be able to change it.
        self.existing = [{
            "id": "story#github#acme/web#42", "type": "story",
            "storyPoints": 5, "title": "Add login",
        }]
        self.run_story("--repo", "acme/web", "--issue", "42",
                       "--points", "8", "--title", "Add SSO login")
        doc = self.spooled()[0]
        self.assertEqual(doc["storyPoints"], 8)
        self.assertEqual(doc["title"], "Add SSO login")

    def test_a_field_nothing_here_knows_about_survives(self):
        # The merge is over every field rather than a named list on purpose:
        # a list is a thing somebody forgets to extend, and the symptom is a
        # field that blanks itself on the next write with no error anywhere.
        self.existing = [{
            "id": "story#github#acme/web#42", "type": "story",
            "somethingAddedLater": "keep me",
        }]
        self.run_story("--repo", "acme/web", "--issue", "42")
        self.assertEqual(self.spooled()[0]["somethingAddedLater"], "keep me")

    # --- the sprint ----------------------------------------------------------

    def test_a_story_with_no_sprint_is_still_recorded(self):
        # Planning must never become a prerequisite for solving: before the
        # first rollover there is no sprint, and the story still exists.
        self.run_story("--repo", "acme/web", "--issue", "42", "--points", "5")
        self.assertIsNone(self.spooled()[0]["sprintId"])

    def test_entering_after_the_sprint_started_is_added_scope(self):
        self.set_sprint(4, "2000-01-01T00:00:00+0000")
        self.run_story("--repo", "acme/web", "--issue", "42", "--points", "5")
        doc = self.spooled()[0]
        self.assertEqual(doc["sprintId"], 4)
        self.assertEqual(doc["sprintScope"], "added")
        self.assertEqual([h["sprintId"] for h in doc["sprintHistory"]], [4])

    def test_a_story_arriving_from_another_sprint_is_carried(self):
        self.existing = [{
            "id": "story#github#acme/web#42", "type": "story",
            "sprintId": 3, "sprintScope": "committed",
            "sprintHistory": [{"sprintId": 3, "scope": "committed",
                               "enteredAt": "2026-07-01T00:00:00+0000"}],
        }]
        self.set_sprint(4, "2000-01-01T00:00:00+0000")
        self.run_story("--repo", "acme/web", "--issue", "42", "--points", "5")
        doc = self.spooled()[0]
        self.assertEqual(doc["sprintId"], 4)
        self.assertEqual(doc["sprintScope"], "carried")
        # Every leg is kept: "was this ever carried?" is asked of the story.
        self.assertEqual([h["sprintId"] for h in doc["sprintHistory"]], [3, 4])

    def test_re_recording_within_the_same_sprint_keeps_the_original_scope(self):
        # A story committed to the sprint that gets re-estimated, or simply
        # gets another solver run, must not be reclassified as added — that
        # is the number the sprint's delivery is judged against.
        self.existing = [{
            "id": "story#github#acme/web#42", "type": "story",
            "sprintId": 4, "sprintScope": "committed",
            "enteredSprintAt": "2026-08-01T00:00:00+0000",
            "sprintHistory": [{"sprintId": 4, "scope": "committed",
                               "enteredAt": "2026-08-01T00:00:00+0000"}],
        }]
        self.set_sprint(4, "2000-01-01T00:00:00+0000")
        self.run_story("--repo", "acme/web", "--issue", "42", "--points", "5")
        doc = self.spooled()[0]
        self.assertEqual(doc["sprintScope"], "committed")
        self.assertEqual(doc["enteredSprintAt"], "2026-08-01T00:00:00+0000")
        self.assertEqual(len(doc["sprintHistory"]), 1)

    def test_no_running_sprint_does_not_evict_a_story_from_its_sprint(self):
        # Between rollovers there is no current sprint. Recording then must
        # not retroactively remove the story from the sprint it was worked in.
        self.existing = [{
            "id": "story#github#acme/web#42", "type": "story",
            "sprintId": 4, "sprintScope": "committed",
            "enteredSprintAt": "2026-08-01T00:00:00+0000",
            "sprintHistory": [{"sprintId": 4, "scope": "committed",
                               "enteredAt": "2026-08-01T00:00:00+0000"}],
        }]
        self.run_story("--repo", "acme/web", "--issue", "42", "--points", "5")
        doc = self.spooled()[0]
        self.assertEqual(doc["sprintId"], 4)
        self.assertEqual(doc["sprintScope"], "committed")

    # --- it cannot fail the run ---------------------------------------------

    def test_a_store_that_raises_costs_the_document_not_the_run(self):
        # Called from a runner's start-up path. Every way of getting it wrong
        # must still leave the work that produced it able to proceed.
        def boom(*a, **k):
            raise RuntimeError("no route to host")
        self.cmd.planning_store.query = boom
        self.assertEqual(
            self.run_story("--repo", "acme/web", "--issue", "42"), 0)

    def test_a_write_that_raises_costs_the_document_not_the_run(self):
        def boom(*a, **k):
            raise RuntimeError("connection reset")
        self.cmd.planning_store.write = boom
        self.assertEqual(
            self.run_story("--repo", "acme/web", "--issue", "42",
                           "--points", "5"), 0)


class TheWiring(unittest.TestCase):
    """A command nothing installs and nothing calls is the original bug.

    `story_doc` and `enter_sprint` existed, were correct, and were tested —
    and had no caller anywhere, so no story was ever written. Both call sites
    guard on `command -v`, which is right (planning must never fail a run) and
    is also exactly what makes losing one silent again: a missing binary reads
    as "not installed on this image" rather than as an error. These three
    assertions are the ones that fail loudly instead.
    """

    def read(self, name):
        with open(os.path.join(BUILDER, name), encoding="utf-8") as f:
            return f.read()

    def test_the_command_ships_in_the_image(self):
        m = re.search(r"COPY --chmod=(\d+) planning-story\s",
                      self.read("Dockerfile"))
        self.assertIsNotNone(
            m, "planning-story is not installed by the Dockerfile — the "
               "`command -v` guard at both call sites would skip it silently")
        self.assertEqual(m.group(1), "0755",
                         "planning-story is a CLI and must ship executable")

    def test_estimation_records_the_story_it_sized(self):
        self.assertIn("planning-story", self.read("estimate-runner.sh"))

    def test_the_solver_records_the_story_it_is_working(self):
        # Not redundant with estimation: an issue that already carries a size
        # is never handed to the estimator again, so without this call every
        # story sized before today, and every story a human sized by hand,
        # would have no document at all.
        self.assertIn("planning-story", self.read("fixer-runner.sh"))


if __name__ == "__main__":
    unittest.main()

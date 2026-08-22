"""Waiting on a person must be visible, and must not hold the repo's slot.

Two distinct failures, both observed:

  #88  The agent decided to ask a human ("blocked on one fact only you can
       read out of the cluster") and ended its turn. Nothing parked the issue.
       It kept `status::in-progress`, stayed fully workable, and held the
       repository's single spawn slot indefinitely — starving every other
       issue in that repository while waiting on someone who had not been told
       they were being waited on.

  #86  The wrapper's own escalation set the awaiting-human MARKER, so the
       planner ranked it last correctly — but wrote no label. The park was
       invisible on GitHub: the board still showed it in progress, and the
       only symptom was that it silently stopped moving.

The marker is what the planner ranks on. The label is what a human sees, and
what they remove to hand the issue back. Both are required.
"""

import os
import unittest

from harness import BUILDER, ShellTestCase

RUNNER = "fixer-runner.sh"


class ParkIsVisibleAndReleasesTheSlot(ShellTestCase):
    def block(self, start, end, name):
        import shutil
        src = self.extract_block(RUNNER, start, end)
        dst = os.path.join(self.home, f"{name}.sh")
        shutil.move(src, dst)
        return f"{name}.sh"

    def preamble(self, labels='[]', last_comment=None, state="open"):
        """A sandbox where the host is the fake seam and state is fixture files.

        The records are in the bot's OWN shape, because that is what the block
        under test now receives — labels are names, an author is a username.
        """
        fixtures = os.path.join(self.home, "fx")
        os.makedirs(fixtures, exist_ok=True)
        import json
        with open(os.path.join(fixtures, "issue_5"), "w") as f:
            json.dump({"number": 5, "state": state,
                       "labels": json.loads(labels)}, f)
        comments = []
        if last_comment:
            comments.append({"id": 1,
                             "author": {"username": last_comment[0]},
                             "body": last_comment[1]})
        with open(os.path.join(fixtures, "comments_5"), "w") as f:
            json.dump(comments, f)
        return "\n".join([
            'set -u',
            'REPO=o/r', 'ISSUE_NUM=5', 'BOT_LOGIN=bot',
            'FORGE=(forge-cli --repo "$REPO")',
            f'AWAITING_HUMAN_MARKER="$PWD/awaiting-human"',
            'repo_owner_login() { echo owner; }',
            f'export FAKE_FORGE_DIR="$PWD/fx"',
            f'export FAKE_FORGE_LOG="$PWD/forge.log"',
        ])

    def test_parking_adds_the_on_hold_label(self):
        blk = self.block("park_on_hold() {", "# Is the newest comment", "park")
        rc, out, err = self.sh("\n".join([
            self.preamble(labels='[]'),
            f'. "{blk}"',
            'park_on_hold',
        ]))
        path = os.path.join(self.home, "forge.log")
        log = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        # The VERB is the assertion. It used to be a URL fragment, which was
        # always standing in for "it added a label" and could only be read by
        # someone who knew the endpoint.
        self.assertIn("add-labels", log,
                      f"no label write was attempted. out={out} err={err}")
        # Defined before applied: at least one host refuses to put an
        # undefined label on an issue, so a park in a fresh repository would
        # silently do nothing without this.
        self.assertIn("ensure-label", log)
        self.assertIn("parked On Hold", out)

    def test_an_already_parked_issue_is_not_relabelled(self):
        # A no-op label write still appends a timeline event, and this runs
        # every five minutes.
        blk = self.block("park_on_hold() {", "# Is the newest comment", "park")
        rc, out, err = self.sh("\n".join([
            self.preamble(labels='["bug", "On Hold"]'),
            f'. "{blk}"',
            'park_on_hold',
        ]))
        self.assertIn("already On Hold", out)
        log_path = os.path.join(self.home, "curl.log")
        log = open(log_path).read() if os.path.exists(log_path) else ""
        self.assertNotIn("POST https://api.github.com/repos/o/r/issues/5/labels", log)

    def test_label_matching_tolerates_spelling(self):
        for spelling in ('["on-hold"]', '["ON HOLD"]', '["On_Hold"]'):
            with self.subTest(spelling=spelling):
                blk = self.block("park_on_hold() {", "# Is the newest comment", "park")
                rc, out, err = self.sh("\n".join([
                    self.preamble(labels=spelling),
                    f'. "{blk}"',
                    'park_on_hold',
                ]))
                self.assertIn("already On Hold", out, spelling)


class DetectingAnUnansweredQuestion(ShellTestCase):
    def block(self, start, end, name):
        import shutil
        src = self.extract_block(RUNNER, start, end)
        dst = os.path.join(self.home, f"{name}.sh")
        shutil.move(src, dst)
        return f"{name}.sh"

    def ask(self, comments):
        import json
        fixtures = os.path.join(self.home, "fx")
        os.makedirs(fixtures, exist_ok=True)
        with open(os.path.join(fixtures, "repos_o_r_issues_5_comments"), "w") as f:
            json.dump(comments, f)
        blk = self.block("bot_awaiting_human_reply() {",
                         "# Park and release the repo lock", "awaiting")
        return self.sh("\n".join([
            'set -u', 'REPO=o/r', 'ISSUE_NUM=5', 'BOT_LOGIN=bot',
            'GH_API=https://api.github.com',
            'AUTH_HEADER="Authorization: Bearer t"',
            'ACCEPT_HEADER="Accept: application/vnd.github+json"',
            'APIV_HEADER="X-GitHub-Api-Version: 2022-11-28"',
            'repo_owner_login() { echo owner; }',
            'export FAKE_CURL_DIR="$PWD/fx"',
            f'. "{blk}"',
            'if bot_awaiting_human_reply; then echo WAITING; else echo NOT_WAITING; fi',
        ]))

    def test_the_bots_unanswered_question_is_a_wait(self):
        rc, out, err = self.ask([{"user": {"login": "bot"},
                                  "body": "@owner blocked on one fact"}])
        self.assertIn("WAITING", out)

    def test_a_human_reply_ends_the_wait(self):
        # The human's comment is newest, so the question is answered.
        rc, out, err = self.ask([
            {"user": {"login": "bot"}, "body": "@owner blocked on one fact"},
            {"user": {"login": "owner"}, "body": "it is nginx"},
        ])
        self.assertIn("NOT_WAITING", out)

    def test_a_bot_comment_with_no_mention_is_not_a_question(self):
        # A status note must not park the issue.
        rc, out, err = self.ask([{"user": {"login": "bot"},
                                  "body": "pushed abc1234, checks running"}])
        self.assertIn("NOT_WAITING", out)

    def test_no_comments_at_all_is_not_a_wait(self):
        rc, out, err = self.ask([])
        self.assertIn("NOT_WAITING", out)


if __name__ == "__main__":
    unittest.main()

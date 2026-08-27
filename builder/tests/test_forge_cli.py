"""The command the shell runners ask their host through.

WHY THE CONTRACT IS THE TEST
----------------------------
Everything on the other side of this is bash, and bash cannot tell the
difference between "the answer is empty" and "there was no answer" unless the
command is careful to. The old shape could not: a failed `curl` printed nothing
and exited into a `|| true`, the empty string went to a `python3 -c`, and an
unreadable issue came out the far end as an open, unlabelled, unestimated one —
which is a description of work, so the runner sent it to the model as work.

So the properties pinned here are the ones a shell caller depends on:

  * stdout carries DATA ONLY. Anything a person reads goes to stderr, because
    `X="$(forge-cli ...)"` captures stdout and hands it to a parser.
  * a failed READ exits non-zero AND prints nothing, so `if ! X="$(...)"` is a
    real gate rather than a formality.
  * a failed WRITE exits non-zero, because every runner reports a write it
    could not do rather than pretending it happened.
  * an empty record is not printed as `{}` — a caller reading that would find
    every field absent and act on it.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import fakeforge
from harness import TMP_ROOT, load_script

import forge


cli = load_script("forge-cli")

REPO = "acme/web"


class CliTestCase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="forgecli-", dir=TMP_ROOT)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.forge = fakeforge.FakeForge(identity="bot")
        self._saved = cli.forge.configured
        cli.forge.configured = lambda env=None: forge.Forges([self.forge])
        self.addCleanup(setattr, cli.forge, "configured", self._saved)

    def run_cli(self, *argv):
        """(exit code, stdout, stderr) — the three things a runner sees."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def issue(self, number=42, **kw):
        record = {"forge": forge.GITHUB, "repo": REPO, "number": number,
                  "title": "Add login", "body": "please", "url": "",
                  "labels": ["bug"], "state": "open", "closedAs": None,
                  "isChangeRequest": False}
        record.update(kw)
        self.forge.issues.append(record)
        return record


class ReadingSomething(CliTestCase):
    def test_an_issue_comes_back_as_json_on_stdout(self):
        self.issue()
        rc, out, err = self.run_cli("--repo", REPO, "issue", "--number", "42")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["title"], "Add login")
        self.assertEqual(err, "")

    def test_an_unreadable_issue_prints_nothing_and_fails(self):
        # The property the whole command exists for. Anything on stdout here
        # would be parsed as an issue by the caller.
        rc, out, err = self.run_cli("--repo", REPO, "issue", "--number", "99")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("99", err)

    def test_a_host_that_raises_is_a_failure_not_an_empty_answer(self):
        self.forge.raises["issue"] = forge.ForgeError("502 on /issues/42")
        rc, out, err = self.run_cli("--repo", REPO, "issue", "--number", "42")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("502", err)

    def test_comments_come_back_as_a_list(self):
        self.forge.notes[42] = [{"id": 1, "body": "hi",
                                 "author": {"username": "someone"}}]
        rc, out, _ = self.run_cli("--repo", REPO, "comments", "--number", "42")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)[0]["author"]["username"], "someone")

    def test_no_comments_is_an_empty_list_and_a_success(self):
        # Distinct from an unreadable issue: nobody has said anything, which
        # is an answer, and the caller loops over it zero times.
        rc, out, _ = self.run_cli("--repo", REPO, "comments", "--number", "42")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [])


class WritingSomething(CliTestCase):
    def body_file(self, text):
        path = os.path.join(self.dir, "body.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_a_comment_body_comes_from_a_file(self):
        rc, _, _ = self.run_cli("--repo", REPO, "comment", "--number", "42",
                                "--body-file", self.body_file("hello there"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.forge.writes,
                         [("comment", REPO, 42, "hello there")])

    def test_a_body_full_of_shell_metacharacters_survives_intact(self):
        # Comment bodies are MODEL OUTPUT. This one would have been a command
        # substitution as an argument, and a review that quotes a shell snippet
        # produces exactly this.
        nasty = "run `rm -rf /` or $(whoami)\nand \"quotes\" 'too'\n"
        rc, _, _ = self.run_cli("--repo", REPO, "comment", "--number", "42",
                                "--body-file", self.body_file(nasty))
        self.assertEqual(rc, 0)
        self.assertEqual(self.forge.writes[0][3], nasty)

    def test_a_failed_comment_exits_non_zero_and_says_so(self):
        self.forge.writes_fail = True
        rc, out, err = self.run_cli("--repo", REPO, "comment", "--number", "42",
                                    "--body-file", self.body_file("hi"))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("42", err)

    def test_labels_are_added_as_a_list(self):
        rc, _, _ = self.run_cli("--repo", REPO, "add-labels", "--number", "42",
                                "--labels", "SP::5, status::in progress ,")
        self.assertEqual(rc, 0)
        self.assertEqual(self.forge.writes,
                         [("labels", REPO, 42, ["SP::5", "status::in progress"])])

    def test_adding_no_labels_at_all_writes_nothing_and_succeeds(self):
        # The plan legitimately comes out empty when the issue already says
        # what the bot was going to say. A failure here would be reported as a
        # labelling error on a run that had nothing to change.
        rc, _, _ = self.run_cli("--repo", REPO, "add-labels", "--number", "42",
                                "--labels", " , ")
        self.assertEqual(rc, 0)
        self.assertEqual(self.forge.writes, [])

    def test_a_label_is_removed_by_the_name_a_person_writes(self):
        # Not pre-encoded. URL-encoding is transport and lives on the other
        # side of this; a runner that encoded it here would double-encode the
        # moment a host wanted it in a body instead of a path.
        rc, _, _ = self.run_cli("--repo", REPO, "remove-label", "--number", "42",
                                "--label", "status::in progress")
        self.assertEqual(self.forge.writes,
                         [("unlabel", REPO, 42, "status::in progress")])
        self.assertEqual(rc, 0)

    def test_defining_a_label_carries_the_colour_and_the_description(self):
        rc, _, _ = self.run_cli("--repo", REPO, "ensure-label",
                                "--name", "SP::5", "--color", "c5def5",
                                "--description", "five points")
        self.assertEqual(rc, 0)
        self.assertEqual(self.forge.writes[0][0], "define-label")
        self.assertEqual(self.forge.writes[0][3],
                         {"name": "SP::5", "color": "c5def5",
                          "description": "five points"})


class TheTwoStreams(CliTestCase):
    """`_out` and `_say` — which stream a thing goes to IS the contract.

    A shell caller writes `X="$(forge-cli ...)"`, which captures stdout and
    nothing else. So a warning printed to stdout does not warn anybody: it
    becomes part of the value, and the next `python3 -c` in the runner is
    handed a diagnostic where it expected a record.
    """

    def test_a_record_goes_to_stdout_as_json(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli._out({"number": 42})
        self.assertEqual(json.loads(out.getvalue()), {"number": 42})
        self.assertEqual(err.getvalue(), "")

    def test_a_bare_word_goes_out_unquoted(self):
        # Reductions like a check state are read straight into a shell
        # variable and compared. JSON-quoting one would make every comparison
        # against `green` fail, silently and forever.
        out = io.StringIO()
        with redirect_stdout(out):
            cli._out("green")
        self.assertEqual(out.getvalue(), "green\n")

    def test_everything_a_person_reads_goes_to_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli._say("could not read acme/web#42")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("could not read", err.getvalue())


class WithNoHostConfigured(CliTestCase):
    def test_it_refuses_rather_than_reporting_an_empty_answer(self):
        # A deployment with no credentials must not look like a repository
        # with no issues. One is a broken install; the other is a quiet day.
        cli.forge.configured = lambda env=None: forge.Forges([])
        rc, out, err = self.run_cli("--repo", REPO, "issue", "--number", "42")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no code host", err)


class RoutingToTheRightHost(CliTestCase):
    def test_the_repository_decides_which_host_is_asked(self):
        other = fakeforge.FakeForge(forge.GITLAB, identity="bot")
        other.issues.append({"forge": forge.GITLAB, "repo": "group/app",
                             "number": 7, "title": "elsewhere", "body": "",
                             "url": "", "labels": [], "state": "open",
                             "closedAs": None, "isChangeRequest": False})
        forges = forge.Forges([self.forge, other])
        forges.remember("group/app", other)
        cli.forge.configured = lambda env=None: forges
        rc, out, _ = self.run_cli("--repo", "group/app", "issue", "--number", "7")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["title"], "elsewhere")


class AskingWhoTheHumanIs(CliTestCase):
    """`owner` is how the shell learns WHO to talk to.

    It used to not ask at all: the runners split `${REPO%%/*}` off the path
    and called that the owner. On a hosted GitLab that first segment is a
    GROUP, and one tester run addressed its findings to all forty-two members
    of one. The verb exists so the answer comes from the host — and so that a
    host with nobody to name can say so.
    """

    def test_one_login_goes_to_stdout(self):
        self.forge.owner = "ada"
        rc, out, err = self.run_cli("--repo", REPO, "owner")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "ada" + chr(10))

    def test_nobody_is_a_failure_and_prints_nothing(self):
        # The shell reads stdout. An empty answer that exited 0 would be
        # interpolated straight after an "@".
        self.forge.owner = ""
        rc, out, err = self.run_cli("--repo", REPO, "owner")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("owner", err)


if __name__ == "__main__":
    unittest.main()

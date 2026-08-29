"""A verdict names the head it reviewed, or it does not get posted.

WHAT THIS IS FOR
A reviewer posted an APPROVED verdict — twice — that belonged to a DIFFERENT
change request in the same repository, reviewed an hour earlier. It carried
that other head's sha and that other change's acceptance criteria, about a
file the change it landed on does not touch. The agent caught it on the next
pass and retracted it, so nothing merged. Had the borrowed verdict been an
APPROVED whose sha the solver's merge gate accepted, the gate would have had
an approval to act on for code no reviewer had read.

The run always knew which head it was reviewing. Nothing compared the comment
against it on the way out.

WHY THE GUARD IS ITS OWN COMMAND
The verdict is posted from two places — the agent runs the poster itself,
because the prompt tells it to, and the wrapper posts from the summary file
when the agent's comment did not land. The bad comments came from the FIRST
of those, so a check inside the wrapper's post_pr_comment would have missed
the very case it was written for. Both paths go through `review-verdict`, and
these tests drive it the way both callers do.

The guard refuses only what it can prove wrong: a verdict header naming a
different commit. An ordinary comment, a verdict with no sha, and a run with
no head on record all pass straight through — a guard that blocked on
uncertainty would be a reviewer that cannot speak.
"""

import json
import os
import unittest

from harness import ShellTestCase

SHA = "6604f14c1055065565068b01ea65fc52948dcb2d"
OTHER = "ba78420d733e87fa8c6ac43bfcff396ff2919281"

MARKER = "\U0001f50e REVIEW RESULT:"


class GuardTestCase(ShellTestCase):
    def setUp(self):
        super().setUp()
        self.env["FAKE_FORGE_DIR"] = "$PWD/fixtures"
        self.env["FAKE_FORGE_LOG"] = "$PWD/forge.log"
        os.makedirs(os.path.join(self.home, "fixtures"), exist_ok=True)

    def post(self, body, head=SHA, on_disk=False, repo="o/r", number=7):
        """Run review-verdict over `body`, as either caller invokes it.

        `head` is how the run says what it is reviewing: through the
        environment normally, or through the state file when the agent's exec
        sandbox did not carry the variable across.
        """
        setup = ""
        if head and on_disk:
            key = f"{repo.replace('/', '__')}__{number}"
            setup = (
                'mkdir -p "$HOME/.openclaw/reviewer-state"\n'
                f'printf %s {head} > '
                f'"$HOME/.openclaw/reviewer-state/{key}.reviewing-sha"\n')
        elif head:
            setup = f'export REVIEW_HEAD_SHA={head}\n'

        with open(os.path.join(self.home, "body.md"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(body)

        return self.sh(
            setup
            + f"review-verdict --repo {repo} --number {number}"
            + " --body-file body.md\n"
            + 'echo "rc=$?"')

    def posted(self):
        """Every call the fake forge recorded."""
        path = os.path.join(self.home, "forge.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]

    def verdict(self, sha, result="APPROVED"):
        return f"{MARKER} {result} (sha {sha})\n\nlooks fine to me\n"


class AVerdictAboutAnotherCommitIsRefused(GuardTestCase):
    def test_nothing_reaches_the_host(self):
        # The assertion that matters: not merely that it exited non-zero, but
        # that the comment was never written. A guard that refused AFTER
        # posting would satisfy an exit-code check and none of the point.
        rc, out, err = self.post(self.verdict(OTHER))
        self.assertIn("rc=1", out, out + err)
        self.assertEqual(self.posted(), [], self.posted())

    def test_it_says_which_sha_it_expected(self):
        # A refusal nobody can act on is a silent failure with extra steps.
        _, _, err = self.post(self.verdict(OTHER))
        self.assertIn("REFUSED", err)
        self.assertIn(OTHER[:12], err)
        self.assertIn(SHA[:12], err)

    def test_the_head_may_come_from_the_state_file(self):
        # The agent posts from inside its own exec sandbox, which does not
        # have to hand every variable through. Losing the head there would
        # turn the guard off exactly where the bad comments came from.
        rc, out, _ = self.post(self.verdict(OTHER), on_disk=True)
        self.assertIn("rc=1", out)
        self.assertEqual(self.posted(), [])

    def test_changes_required_is_guarded_too(self):
        # Not only approvals: a borrowed CHANGES REQUIRED blocks a merge on
        # findings about somebody else's diff.
        rc, out, _ = self.post(self.verdict(OTHER, "CHANGES REQUIRED"))
        self.assertIn("rc=1", out)
        self.assertEqual(self.posted(), [])


class TheOrdinaryCasesStillPost(GuardTestCase):
    def test_the_matching_head_posts(self):
        rc, out, err = self.post(self.verdict(SHA))
        self.assertIn("rc=0", out, out + err)
        self.assertTrue(
            any(p.startswith("comment-on-change-request") for p in self.posted()),
            self.posted())

    def test_an_abbreviated_sha_agrees_with_the_full_one(self):
        # The wrapper writes the full forty characters; a model writing its
        # own header usually writes seven. Both are the same commit, and a
        # guard that called them different would block every hand-written
        # verdict in the project.
        rc, out, _ = self.post(self.verdict(SHA[:7]))
        self.assertIn("rc=0", out)
        self.assertTrue(self.posted())

    def test_a_verdict_with_no_sha_is_not_blocked(self):
        rc, out, _ = self.post(f"{MARKER} APPROVED\n\nno sha here\n")
        self.assertIn("rc=0", out)
        self.assertTrue(self.posted())

    def test_an_ordinary_comment_is_not_a_verdict(self):
        # Only the verdict header is checked. Everything else the reviewer
        # says — questions, notes, the request-for-review acknowledgement —
        # goes through untouched, including text that merely mentions a sha.
        rc, out, _ = self.post(f"just a note about {OTHER}, nothing formal\n")
        self.assertIn("rc=0", out)
        self.assertTrue(self.posted())

    def test_no_head_on_record_means_no_opinion(self):
        # A verdict posted outside a run has nothing to be checked against.
        # The guard passes what it cannot prove wrong rather than inventing a
        # reason to block.
        rc, out, _ = self.post(self.verdict(OTHER), head=None)
        self.assertIn("rc=0", out)
        self.assertTrue(self.posted())


class TheHeaderIsReadHoweverTheModelWroteIt(GuardTestCase):
    """The shapes a model actually reaches for, not the one the prompt asks
    for.

    The header is model output. `test_verdict_sha_parsing` records what that
    means in practice — markdown backticks far more often than the bare form,
    plus quotes and `sha:` — and the deadlock that taught the solver to accept
    them all.

    A guard matching only the bare form would find no sha in the common case,
    treat the verdict as unlabelled and wave it through. That is worse than no
    guard: it reports enforcement it is not doing.
    """

    def wrong(self, header):
        rc, out, _ = self.post(f"{header}\n\nbody\n")
        self.assertIn("rc=1", out, header)
        self.assertEqual(self.posted(), [], header)

    def test_a_sha_in_markdown_backticks_is_read(self):
        # The form these headers arrive in most often.
        self.wrong(f"{MARKER} APPROVED (sha `{OTHER}`)")

    def test_the_other_shapes_a_model_reaches_for(self):
        for header in (
            f"{MARKER} APPROVED (sha: `{OTHER}`)",
            f'{MARKER} APPROVED (sha "{OTHER}")',
            f"{MARKER} APPROVED (sha  '{OTHER}' )",
            f"{MARKER} APPROVED (SHA {OTHER})",
        ):
            with self.subTest(header=header[:56]):
                # No re-setUp: `wrong` asserts the forge log is EMPTY, so a
                # shared sandbox across the loop can only make that stricter.
                self.wrong(header)

    def test_an_abbreviated_foreign_sha_is_still_caught(self):
        self.wrong(f"{MARKER} APPROVED (sha `{OTHER[:7]}`)")

    def test_the_matching_head_in_backticks_still_posts(self):
        rc, out, _ = self.post(f"{MARKER} APPROVED (sha `{SHA}`)\n\nok\n")
        self.assertIn("rc=0", out)
        self.assertTrue(self.posted())

    def test_prose_is_not_mistaken_for_a_commit(self):
        # Six hex characters is below the abbreviation floor. Reading it as a
        # sha would block a verdict over a word.
        rc, out, _ = self.post(f"{MARKER} APPROVED (sha beefed)\n\nok\n")
        self.assertIn("rc=0", out)
        self.assertTrue(self.posted())


class TheRunIsNotAbortedByARefusal(GuardTestCase):
    def test_a_refusal_leaves_the_next_post_free_to_succeed(self):
        # Criterion 3 of the issue: refusing one comment must not end the
        # review. The runner treats a failed post as "the comment did not
        # land" and carries on, so the correct verdict can still go out.
        self.post(self.verdict(OTHER))
        self.assertEqual(self.posted(), [])
        rc, out, _ = self.post(self.verdict(SHA))
        self.assertIn("rc=0", out)
        self.assertTrue(self.posted())


class TheGuardIsWiredIn(unittest.TestCase):
    """Both posters go through it, and it ships. Either half alone is a
    guard that is present and never reached."""

    def setUp(self):
        from harness import BUILDER
        self.builder = BUILDER
        with open(os.path.join(BUILDER, "reviewer-runner.sh"),
                  encoding="utf-8") as f:
            self.runner = f.read()

    def test_the_wrapper_posts_through_the_guard(self):
        self.assertIn("review-verdict --repo", self.runner)

    def test_the_agent_is_told_to_use_it(self):
        # The prompt is the only thing standing between the agent and a bare
        # forge-cli call, and the agent's post is the path the bad comments
        # actually took.
        prompt = self.runner[self.runner.index("## Verdict — HOW TO REPORT"):]
        head = prompt[:2000]
        self.assertIn("review-verdict --repo $REPO", head)
        self.assertNotIn("forge-cli --repo $REPO comment-on-change-request",
                         head)

    def test_it_is_installed_in_the_image(self):
        with open(os.path.join(self.builder, "Dockerfile"),
                  encoding="utf-8") as f:
            self.assertIn("review-verdict", f.read())

    def test_it_ships_in_the_runner_configmap(self):
        # Same reason every other runner script is listed: without it the
        # ConfigMap copy is missing and the pod silently falls back to
        # whatever the image last baked.
        with open(os.path.join(self.builder, "kustomization.yaml"),
                  encoding="utf-8") as f:
            self.assertIn("review-verdict=review-verdict", f.read())


if __name__ == "__main__":
    unittest.main()

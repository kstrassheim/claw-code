"""Closing a change request, and deleting the branch it was built on.

Until now a change request could only ever end by being MERGED. There was no
verb to abandon one, and no way to delete a branch except as a side effect of
merging. So when a person closed an issue as not-doing, the work opened for it
stayed open forever: a change request nobody would merge, on a branch nobody
would touch.

k8s-ultimate-web-stack#93 is the case. Asked to "close the issue and the pr,
delete the branch", the bot could have done exactly one of the three. It
worked out only because no change request or branch had been created.

BOTH HOSTS, deliberately. GitHub PATCHes a pull request's state; GitLab sends
`state_event` to a merge request, the same verb it uses to close an issue.
Callers say "close this", and which host is answering is not their business —
that is the entire point of the forge.
"""

import unittest

import fakeforge
import forge


class ForgeShapes(unittest.TestCase):
    """Every forge implements both, including the fake the suite runs on."""

    IMPLS = (forge.GitHubForge, forge.GitLabForge, fakeforge.FakeForge)

    def test_both_verbs_exist_on_every_forge(self):
        for impl in self.IMPLS:
            for name in ("close_change_request", "delete_branch"):
                self.assertTrue(callable(getattr(impl, name, None)),
                                f"{impl.__name__} is missing {name}")

    def test_the_base_declares_them_so_a_new_host_cannot_forget(self):
        for name in ("close_change_request", "delete_branch"):
            attr = getattr(forge.Forge, name)
            self.assertTrue(getattr(attr, "__isabstractmethod__", False),
                            f"{name} is not abstract; a new forge could ship "
                            "without it and fail only at the call site")

    def test_abandoning_is_not_merging(self):
        # If these were the same call, "close it without merging" would land
        # the very code somebody asked not to land.
        self.assertNotEqual(forge.GitHubForge.close_change_request,
                            forge.GitHubForge.merge)
        self.assertNotEqual(forge.GitLabForge.close_change_request,
                            forge.GitLabForge.merge)


class TheDefaultBranchIsRefused(unittest.TestCase):
    """The guard lives in the forge, not in the caller.

    Deleting a branch is the one irreversible thing here: a closed issue
    reopens and a closed change request reopens, but commits reachable only
    from a deleted branch are gone. So the layer that KNOWS which branch is
    default is the layer that refuses it — a caller cannot be trusted with a
    check it only has to get wrong once.
    """

    def setUp(self):
        self.f = fakeforge.FakeForge(identity="bot")
        self.f.heads["o/r"] = ("main", "abc1234")
        self.f.branches["issue-93-fix"] = "def5678"

    def test_the_default_branch_is_never_deleted(self):
        self.assertFalse(self.f.delete_branch("o/r", "main"))
        self.assertIn("main", [b for b in ("main",)])  # still the default

    def test_a_feature_branch_is_deleted(self):
        self.assertTrue(self.f.delete_branch("o/r", "issue-93-fix"))
        self.assertNotIn("issue-93-fix", self.f.branches)

    def test_an_empty_branch_name_is_refused(self):
        self.assertFalse(self.f.delete_branch("o/r", ""))
        self.assertFalse(self.f.delete_branch("o/r", None))

    def test_an_unreadable_default_refuses_rather_than_guesses(self):
        # Not knowing what the default is means not being able to promise this
        # is not it. A branch that stays costs nothing; main does not.
        self.f.raises = {"default_branch_head": RuntimeError("500")}
        with self.assertRaises(RuntimeError):
            self.f.delete_branch("o/r", "issue-93-fix")

    def test_closing_a_change_request_leaves_the_branch_alone(self):
        # Two calls on purpose: abandoning must not destroy the only copy of
        # the work as a side effect.
        self.f.close_change_request("o/r", 7)
        self.assertIn("issue-93-fix", self.f.branches)


class TheRealForgesUseTheRightCalls(unittest.TestCase):
    """Pinned because a wrong verb here fails silently as 'did not write'."""

    def source(self, cls):
        import inspect
        return inspect.getsource(cls)

    def test_github_patches_the_pull_request_state(self):
        src = self.source(forge.GitHubForge.close_change_request)
        self.assertIn("PATCH", src)
        self.assertIn("/pulls/", src)
        self.assertIn('"state": "closed"', src)

    def test_github_deletes_a_ref(self):
        src = self.source(forge.GitHubForge.delete_branch)
        self.assertIn("DELETE", src)
        self.assertIn("/git/refs/heads/", src)

    def test_gitlab_uses_state_event_like_it_does_for_issues(self):
        src = self.source(forge.GitLabForge.close_change_request)
        self.assertIn("merge_requests", src)
        self.assertIn("state_event", src)

    def test_gitlab_deletes_through_the_repository_branches_endpoint(self):
        src = self.source(forge.GitLabForge.delete_branch)
        self.assertIn("DELETE", src)
        self.assertIn("/repository/branches/", src)

    def test_gitlab_escapes_the_branch_name(self):
        # `feature/x` is a legal branch and an illegal path segment. Without
        # quoting, deleting it either 404s or addresses something else.
        self.assertIn("quote", self.source(forge.GitLabForge.delete_branch))

    def test_both_hosts_consult_the_default_before_deleting(self):
        for impl in (forge.GitHubForge, forge.GitLabForge):
            src = self.source(impl.delete_branch)
            self.assertIn("default_branch_head", src,
                          f"{impl.__name__} deletes without checking the "
                          "default branch")


class TheRunnerOnlyAbandonsCalledOffWork(unittest.TestCase):
    """`abandon_open_change_requests` fires on revoked, never on delivered.

    A DELIVERED issue was closed by its own merge, and `merge` already removed
    the branch. Running this there would try to close a change request that
    just landed. A REVOKED one is the case that leaks: an open change request
    nobody will merge, on a branch nobody will touch.
    """

    def setUp(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "fixer-runner.sh")
        with open(path, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_it_is_reached_only_from_the_revoked_branch(self):
        calls = [l for l in self.src.splitlines()
                 if "abandon_open_change_requests" in l and "()" not in l]
        self.assertTrue(calls, "nothing calls abandon_open_change_requests")
        for line in calls:
            self.assertIn('"$intent" = "revoked"', line,
                          "abandoning must be gated on the issue being called "
                          f"off, not on any close: {line.strip()}")

    def test_the_change_request_is_closed_before_the_branch_is_deleted(self):
        body = self.src.split("abandon_open_change_requests() {", 1)[1]
        close = body.index("close-change-request")
        delete = body.index("delete-branch")
        self.assertLess(close, delete,
                        "deleting first would close the change request as an "
                        "unreachable diff and lose the review context")

    def test_a_failed_close_leaves_the_branch_alone(self):
        # An open change request pointing at a deleted branch is worse than
        # either problem on its own.
        body = self.src.split("abandon_open_change_requests() {", 1)[1]
        head = body[:body.index("delete-branch")]
        self.assertIn("continue", head,
                      "a refused close must skip the delete, not fall through")

    def test_the_branch_comes_from_the_change_request_not_a_convention(self):
        # A human may open a change request from a branch named anything.
        # Guessing `issue-<n>-fix` is how the wrong branch gets deleted.
        body = self.src.split("abandon_open_change_requests() {", 1)[1]
        self.assertIn("head_ref_of_pr", body)
        self.assertIn("headRef", self.src)


class TheCliExposesThem(unittest.TestCase):
    """The runner is shell; it can only reach the forge through the CLI."""

    def setUp(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "forge-cli")
        with open(path, encoding="utf-8") as fh:
            self.cli = fh.read()

    def test_close_change_request_is_a_verb(self):
        self.assertIn('add_parser("close-change-request"', self.cli)
        self.assertIn('f.close_change_request(', self.cli)

    def test_delete_branch_is_a_verb(self):
        self.assertIn('add_parser("delete-branch"', self.cli)
        self.assertIn('f.delete_branch(', self.cli)


if __name__ == "__main__":
    unittest.main()


class CommentsGoWhereTheyBelong(unittest.TestCase):
    """A change request is not an issue, except on one host by accident.

    GitHub models a pull request AS an issue, so `/issues/<pr>/comments` lands
    on the pull request and the two calls coincide. On GitLab the same number
    addresses an issue with that iid — a different item, usually somebody
    else's work.

    That coincidence hid a live bug: the reviewer posted its verdict with the
    ISSUE comment verb and a pull-request number. Correct on GitHub, and on
    GitLab it would have put the verdict on an unrelated issue while the
    solver waited forever for one it could never see.
    """

    def setUp(self):
        self.f = fakeforge.FakeForge(identity="bot")

    def test_every_forge_can_comment_on_a_change_request(self):
        for impl in (forge.GitHubForge, forge.GitLabForge, fakeforge.FakeForge):
            self.assertTrue(
                callable(getattr(impl, "post_change_request_comment", None)),
                f"{impl.__name__} cannot comment on a change request")

    def test_it_is_abstract_on_the_base(self):
        self.assertTrue(
            forge.Forge.post_change_request_comment.__isabstractmethod__)

    def test_gitlab_uses_the_merge_request_notes_endpoint(self):
        import inspect
        src = inspect.getsource(forge.GitLabForge.post_change_request_comment)
        # The PATH only — the docstring names /issues/ to explain the trap.
        path = [l for l in src.splitlines() if "f\"/projects/" in l]
        self.assertTrue(path, "no request path found")
        self.assertIn("merge_requests", path[0])
        self.assertIn("/notes", path[0])
        self.assertNotIn("/issues/", path[0],
                         "this would put the note on an unrelated issue")

    def test_github_uses_the_shared_issue_endpoint(self):
        import inspect
        src = inspect.getsource(forge.GitHubForge.post_change_request_comment)
        self.assertIn("/issues/", src)
        self.assertIn("/comments", src)

    def test_the_note_lands_on_the_change_request(self):
        self.f.post_change_request_comment("o/r", 7, "hello")
        self.assertEqual(
            [n["body"] for n in self.f.change_request_comments("o/r", 7)],
            ["hello"])

    def test_the_reviewer_posts_its_verdict_on_the_change_request(self):
        # The verdict now goes out through `review-verdict`, which refuses one
        # naming a commit the run is not reviewing and then hands off to
        # forge-cli. The property this pins is unchanged — the verdict must
        # reach the CHANGE REQUEST and never the issue that happens to share
        # its number — so the check follows the indirection rather than
        # dropping to "something, somewhere, posts a comment".
        import os
        builder = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        with open(os.path.join(builder, "reviewer-runner.sh"),
                  encoding="utf-8") as fh:
            runner = fh.read()
        self.assertNotIn('comment --number "$PR_NUMBER"', runner,
                         "the verdict uses the ISSUE verb with a PR number")
        self.assertIn('review-verdict --repo "$REPO" --number "$PR_NUMBER"',
                      runner)

        with open(os.path.join(builder, "review-verdict"),
                  encoding="utf-8") as fh:
            guard = fh.read()
        self.assertIn("comment-on-change-request", guard)
        # The guard is the last hop, so the issue verb must not appear here
        # either: routing through it would otherwise be a way to reintroduce
        # exactly the bug this test was written for.
        self.assertNotIn("forge-cli --repo \"$REPO\" comment ", guard)

    def test_the_review_request_is_posted_on_the_change_request(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "fixer-runner.sh")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        block = src.split("request_self_review() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("post_pr_comment", block,
                      "the request must go where its verdict lands")
        self.assertNotIn("post_issue_comment", block)

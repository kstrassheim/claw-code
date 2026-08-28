"""The URL git is handed must carry a credential, and name the right host.

THE INCIDENT THIS GUARDS
Every runner built its own clone URL and hardcoded it:

    https://x-access-token:${GITHUB_TOKEN}@github.com/$REPO.git

On a GitLab-only deployment — GITLAB_URL and GITLAB_API_TOKEN set,
GITHUB_TOKEN empty, which is exactly how prod is configured — that is the
wrong host AND an empty credential. A runner is a background subprocess with
stdin closed, so git could not fall back to asking:

    fatal: could not read Username for 'https://gl.invalid':
    No such device or address

Nothing treated that as fatal. `git remote show origin` returned nothing, so
the default branch was read as the EMPTY STRING and the run carried on:

    [checkout] default-branch=
    fatal: empty string is not a valid pathspec
    fatal: ambiguous argument 'origin/': unknown revision

The API half of the bot was forge-aware throughout; only the git transport
had never learned there was more than one host. It logged 2,096 credential
failures in a single run while still reporting itself as working.

WHAT IS ASSERTED
That each host spells its own credential the way that host actually accepts,
that a token with URL-significant characters cannot reshape the URL, and that
a missing token degrades to an anonymous URL rather than to `user:@host` —
which git offers, the host rejects, and a reader misdiagnoses as a bad token
rather than as no token at all.
"""

from __future__ import annotations

import os
import unittest

import harness  # noqa: F401 - puts builder/ on sys.path

from harness import BUILDER

import forge
from forge_azdo import AzureDevOpsForge
from forge_gitea import GiteaForge
from forge_github import GitHubForge
from forge_gitlab import GitLabForge


class EachHostSpellsItsOwnCredential(unittest.TestCase):
    def test_gitlab_uses_oauth2_as_the_user_half(self):
        f = GitLabForge("https://gl.invalid", "tok")
        self.assertEqual(
            "https://oauth2:tok@gl.invalid/group/team/app.git",
            f.clone_url("group/team/app"))

    def test_github_uses_x_access_token_and_the_web_host(self):
        # The API lives on api.github.com; git does not.
        f = GitHubForge("tok")
        self.assertEqual("https://x-access-token:tok@github.com/o/n.git",
                         f.clone_url("o/n"))

    def test_github_enterprise_drops_the_api_suffix(self):
        f = GitHubForge("tok", "https://ghe.example.com/api/v3")
        self.assertEqual("https://x-access-token:tok@ghe.example.com/o/n.git",
                         f.clone_url("o/n"))

    def test_gitea_puts_the_token_in_the_user_half_alone(self):
        f = GiteaForge("https://gitea.example.com", "tok")
        self.assertEqual("https://tok@gitea.example.com/o/n.git",
                         f.clone_url("o/n"))

    def test_azure_devops_uses_its_own_path_shape(self):
        # Not <base>/<repo>.git: the project is its own segment, `_git` sits
        # between, and there is no .git suffix.
        f = AzureDevOpsForge("https://dev.azure.com/org", "tok")
        self.assertEqual("https://pat:tok@dev.azure.com/org/proj/_git/repo",
                         f.clone_url("proj/repo"))


class ATokenCannotReshapeTheUrl(unittest.TestCase):
    """A PAT is not hex. One containing `@`, `/` or `:` pasted in raw moves
    the host, and the failure names a host nobody recognises."""

    def test_the_token_is_percent_encoded(self):
        f = GitLabForge("https://gl.invalid", "gl/pat@weird:1")
        url = f.clone_url("g/p")
        self.assertEqual(
            "https://oauth2:gl%2Fpat%40weird%3A1@gl.invalid/g/p.git", url)
        # Exactly one @ — the one separating credential from host.
        self.assertEqual(1, url.count("@"))

    def test_the_host_survives_a_token_full_of_slashes(self):
        f = GitLabForge("https://gl.invalid", "a/b/c/d")
        self.assertTrue(
            f.clone_url("g/p").endswith("@gl.invalid/g/p.git"))


class NoTokenIsAnonymousNotEmptyPassword(unittest.TestCase):
    def test_gitlab_without_a_token_is_anonymous(self):
        f = GitLabForge("https://gl.invalid", "")
        self.assertEqual("https://gl.invalid/g/p.git", f.clone_url("g/p"))

    def test_github_without_a_token_is_anonymous(self):
        # The prod configuration: GITHUB_TOKEN empty. The old code produced
        # `https://x-access-token:@github.com/...` here.
        f = GitHubForge("")
        url = f.clone_url("o/n")
        self.assertEqual("https://github.com/o/n.git", url)
        self.assertNotIn(":@", url)

    def test_azure_devops_without_a_token_is_anonymous(self):
        f = AzureDevOpsForge("https://dev.azure.com/org", "")
        self.assertEqual("https://dev.azure.com/org/proj/_git/repo",
                         f.clone_url("proj/repo"))


class NonsenseIsEmptyRatherThanAMalformedUrl(unittest.TestCase):
    """`forge-cli clone-url` treats "" as a failure and says so. A URL built
    from half an answer would be handed to git and fail less legibly."""

    def test_no_repo_is_empty(self):
        self.assertEqual("", GitLabForge("https://h", "t").clone_url(""))

    def test_azure_devops_needs_both_project_and_repo(self):
        f = AzureDevOpsForge("https://dev.azure.com/org", "t")
        self.assertEqual("", f.clone_url("just-a-repo"))


class TheSharedAssemblerItself(unittest.TestCase):
    """`_with_credential` is what three of the four hosts delegate to, so its
    edges are tested here once rather than three times through them."""

    def test_a_base_with_no_scheme_is_assumed_https(self):
        self.assertEqual("https://oauth2:t@h.example/g/p.git",
                         forge._with_credential("h.example", "oauth2", "t", "g/p"))

    def test_a_trailing_slash_on_the_base_does_not_double_up(self):
        self.assertEqual("https://oauth2:t@h.example/g/p.git",
                         forge._with_credential("https://h.example/", "oauth2",
                                                "t", "g/p"))

    def test_a_leading_slash_on_the_repo_does_not_double_up(self):
        self.assertEqual("https://oauth2:t@h.example/g/p.git",
                         forge._with_credential("https://h.example", "oauth2",
                                                "t", "/g/p"))

    def test_a_base_carrying_a_path_keeps_it(self):
        # Azure DevOps aside, a self-hosted instance can live under a prefix.
        self.assertEqual("https://oauth2:t@h.example/git/g/p.git",
                         forge._with_credential("https://h.example/git",
                                                "oauth2", "t", "g/p"))

    def test_the_scheme_is_preserved(self):
        self.assertTrue(forge._with_credential("http://h.example", "u", "t",
                                               "g/p").startswith("http://"))

    def test_an_empty_base_or_repo_is_empty(self):
        self.assertEqual("", forge._with_credential("", "u", "t", "g/p"))
        self.assertEqual("", forge._with_credential("https://h", "u", "t", ""))

    def test_the_user_half_is_encoded_too(self):
        # Not expected to need it, but a user half with an `@` would move the
        # host exactly as an unencoded token would.
        self.assertEqual("https://a%40b:t@h.example/g/p.git",
                         forge._with_credential("https://h.example", "a@b",
                                                "t", "g/p"))


class NoRunnerSpellsAHostItself(unittest.TestCase):
    """The fix is only durable if the next runner asks too.

    All three had their own copy of the URL, so the same defect had to be
    found and fixed three times. A fourth copy would be silent again on any
    deployment whose host is not github.com.
    """

    RUNNERS = ("fixer-runner.sh", "reviewer-runner.sh", "tester-runner.sh")

    def _source(self, name):
        with open(os.path.join(BUILDER, name), encoding="utf-8") as fh:
            return fh.read()

    def test_no_runner_hardcodes_a_git_host(self):
        for name in self.RUNNERS:
            src = self._source(name)
            for line in src.splitlines():
                bare = line.strip()
                if bare.startswith("#"):
                    continue          # prose about the bug is allowed to name it
                self.assertNotIn(
                    "github.com/$REPO", bare,
                    f"{name} builds its own clone URL again: {bare!r}")

    def test_every_runner_asks_the_forge(self):
        for name in self.RUNNERS:
            self.assertIn("clone-url", self._source(name),
                          f"{name} never asks the forge for a clone url")

    def test_every_runner_repairs_an_existing_remote(self):
        # The workspace volume outlives the fix: checkouts cloned by the old
        # runner keep the credential-less URL, and a repair that only happens
        # inside the `if [ ! -d .git ]` clone branch never reaches them.
        for name in self.RUNNERS:
            self.assertIn("remote set-url origin", self._source(name),
                          f"{name} leaves an existing checkout on its old URL")


class TheInterfaceRequiresIt(unittest.TestCase):
    def test_clone_url_is_abstract(self):
        # Every host must answer this. A host that inherited a default would
        # hand back a github.com URL again, which is the original bug.
        self.assertIn("clone_url", forge.Forge.__abstractmethods__)


if __name__ == "__main__":
    unittest.main()

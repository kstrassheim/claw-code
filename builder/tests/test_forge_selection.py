"""Which host answers a question, and what happens when none is configured.

THE RULE THIS PINS
------------------
Selection is PER ITEM, from where the item was discovered — never a
deployment-wide switch. That distinction is the whole reason the bot can work
two hosts in the same tick: a switch would force a deployment to choose one of
them, which is exactly the limitation this seam exists to remove.

And the other half, which is what protects everything that already works: a
deployment with one set of credentials constructs one implementation and
behaves precisely as it did before there were two. A host whose credentials
are unset is skipped SILENTLY — not configured is the normal state, not a
fault to report.
"""

import unittest

from harness import load  # noqa: F401 - puts builder/ on sys.path

import forge  # noqa: E402
import fakeforge  # noqa: E402


class Configuration(unittest.TestCase):
    def test_a_deployment_with_only_github_builds_only_github(self):
        forges = forge.configured({"GITHUB_TOKEN": "t"})
        self.assertEqual(forges.kinds(), [forge.GITHUB])

    def test_a_deployment_with_only_gitlab_builds_only_gitlab(self):
        forges = forge.configured({"GITLAB_URL": "https://g.invalid",
                                   "GITLAB_API_TOKEN": "t"})
        self.assertEqual(forges.kinds(), [forge.GITLAB])

    def test_both_sets_of_credentials_build_both(self):
        forges = forge.configured({"GITHUB_TOKEN": "t",
                                   "GITLAB_URL": "https://g.invalid",
                                   "GITLAB_API_TOKEN": "t"})
        self.assertEqual(forges.kinds(), [forge.GITHUB, forge.GITLAB])

    def test_half_a_set_of_gitlab_credentials_is_not_a_host(self):
        # A URL with no token cannot answer anything, and a forge that raises
        # on every question is worse than one that was never built.
        self.assertEqual(
            forge.configured({"GITLAB_URL": "https://g.invalid"}).kinds(), [])
        self.assertEqual(
            forge.configured({"GITLAB_API_TOKEN": "t"}).kinds(), [])

    def test_a_deployment_with_only_gitea_builds_only_gitea(self):
        forges = forge.configured({"GITEA_URL": "https://t.invalid",
                                   "GITEA_API_TOKEN": "t"})
        self.assertEqual(forges.kinds(), [forge.GITEA])

    def test_half_a_set_of_gitea_credentials_is_not_a_host(self):
        self.assertEqual(
            forge.configured({"GITEA_URL": "https://t.invalid"}).kinds(), [])
        self.assertEqual(
            forge.configured({"GITEA_API_TOKEN": "t"}).kinds(), [])

    def test_all_three_sets_of_credentials_build_all_three(self):
        forges = forge.configured({"GITHUB_TOKEN": "t",
                                   "GITLAB_URL": "https://g.invalid",
                                   "GITLAB_API_TOKEN": "t",
                                   "GITEA_URL": "https://t.invalid",
                                   "GITEA_API_TOKEN": "t"})
        self.assertEqual(forges.kinds(),
                         [forge.GITHUB, forge.GITLAB, forge.GITEA])

    def test_adding_gitea_does_not_disturb_a_deployment_without_it(self):
        # The whole promise of the seam: a deployment that has never heard of
        # this host constructs exactly what it constructed before, and a
        # missing set of credentials is not a fault to report.
        for env in ({"GITHUB_TOKEN": "t"},
                    {"GITLAB_URL": "https://g.invalid",
                     "GITLAB_API_TOKEN": "t"}):
            with self.subTest(env=sorted(env)):
                self.assertNotIn(forge.GITEA, forge.configured(env).kinds())

    def test_a_gitea_base_keeps_its_own_api_root(self):
        forges = forge.configured({"GITEA_URL": "https://t.invalid/",
                                   "GITEA_API_TOKEN": "t"})
        self.assertEqual(forges.by_kind(forge.GITEA).api,
                         "https://t.invalid/api/v1")

    def test_a_deployment_with_only_azure_devops_builds_only_azure_devops(self):
        forges = forge.configured({"AZDO_ORG_URL": "https://dev.azure.com/acme",
                                   "AZDO_API_TOKEN": "t"})
        self.assertEqual(forges.kinds(), [forge.AZDO])

    def test_the_bare_organisation_name_is_enough(self):
        # AZDO_ORG is what the CLI and the MCP server take, so a deployment
        # sets one value rather than a name and a URL that must agree.
        forges = forge.configured({"AZDO_ORG": "acme", "AZDO_API_TOKEN": "t"})
        self.assertEqual(forges.kinds(), [forge.AZDO])
        self.assertEqual(forges.by_kind(forge.AZDO).url,
                         "https://dev.azure.com/acme")

    def test_half_a_set_of_azure_devops_credentials_is_not_a_host(self):
        self.assertEqual(
            forge.configured({"AZDO_ORG_URL": "https://dev.azure.com/acme"}).kinds(), [])
        self.assertEqual(
            forge.configured({"AZDO_API_TOKEN": "t"}).kinds(), [])
        self.assertEqual(
            forge.configured({"AZDO_ORG": "acme"}).kinds(), [])

    def test_all_four_sets_of_credentials_build_all_four(self):
        forges = forge.configured({"GITHUB_TOKEN": "t",
                                   "GITLAB_URL": "https://g.invalid",
                                   "GITLAB_API_TOKEN": "t",
                                   "GITEA_URL": "https://t.invalid",
                                   "GITEA_API_TOKEN": "t",
                                   "AZDO_ORG_URL": "https://dev.azure.com/acme",
                                   "AZDO_API_TOKEN": "t"})
        self.assertEqual(forges.kinds(),
                         [forge.GITHUB, forge.GITLAB, forge.GITEA, forge.AZDO])

    def test_adding_azure_devops_does_not_disturb_a_deployment_without_it(self):
        for env in ({"GITHUB_TOKEN": "t"},
                    {"GITEA_URL": "https://t.invalid", "GITEA_API_TOKEN": "t"}):
            with self.subTest(env=sorted(env)):
                self.assertNotIn(forge.AZDO, forge.configured(env).kinds())

    def test_no_credentials_at_all_is_no_host_and_not_a_crash(self):
        forges = forge.configured({})
        self.assertEqual(len(forges), 0)
        self.assertFalse(forges)

    def test_the_api_base_is_overridable(self):
        # Same substitution an enterprise install needs, and the same one a
        # test fixture uses.
        forges = forge.configured({"GITHUB_TOKEN": "t",
                                   "GITHUB_API": "https://gh.internal/api"})
        self.assertEqual(forges.by_kind(forge.GITHUB).api,
                         "https://gh.internal/api")


class Routing(unittest.TestCase):
    def setUp(self):
        self.gh = fakeforge.FakeForge(forge.GITHUB)
        self.gl = fakeforge.FakeForge(forge.GITLAB, noun="merge request")
        self.forges = forge.Forges([self.gh, self.gl])

    def test_an_item_is_routed_by_the_stamp_discovery_left_on_it(self):
        self.assertIs(self.forges.of({"forge": forge.GITLAB, "repo": "g/app"}),
                      self.gl)
        self.assertIs(self.forges.of({"forge": forge.GITHUB, "repo": "o/r"}),
                      self.gh)

    def test_two_repositories_in_one_tick_go_to_different_hosts(self):
        # The property a global switch cannot have.
        self.gh.issues = [{"forge": forge.GITHUB, "repo": "o/r", "number": 1}]
        self.gl.issues = [{"forge": forge.GITLAB, "repo": "g/app", "number": 2}]
        by_repo = self.forges.assigned_open_issues()
        self.assertEqual(sorted(by_repo), ["g/app", "o/r"])
        self.assertIs(self.forges.of("o/r"), self.gh)
        self.assertIs(self.forges.of("g/app"), self.gl)

    def test_a_repository_name_alone_is_routed_by_where_it_was_discovered(self):
        # The tester's candidates are names off the owner's permitted list and
        # carry no stamp, so what discovery recorded earlier in the tick is
        # what answers.
        self.gl.repos = ["g/app"]
        self.forges.accessible_repos(8)
        self.assertIs(self.forges.of("g/app"), self.gl)

    def test_an_unknown_repository_falls_back_to_the_first_host(self):
        # With one host configured this is the only possible answer and is
        # exactly today's behaviour; with two it is a guess, which is why
        # discovery records what it finds.
        self.assertIs(self.forges.of("never/seen"), self.gh)

    def test_asking_with_no_host_configured_is_an_error_not_a_shrug(self):
        # A caller acting on "no forge" would silently do nothing, every tick,
        # and look idle rather than broken.
        with self.assertRaises(forge.ForgeError):
            forge.Forges([]).of("o/r")

    def test_the_review_candidates_of_every_host_are_merged(self):
        self.gh.candidates = [{"forge": forge.GITHUB, "repo": "o/r",
                               "number": 1}]
        self.gl.candidates = [{"forge": forge.GITLAB, "repo": "g/app",
                               "number": 2}]
        got = self.forges.reviewable_change_requests(8)
        self.assertEqual([(i["repo"], i["number"]) for i in got],
                         [("o/r", 1), ("g/app", 2)])

    def test_the_cap_still_holds_across_hosts(self):
        # One busy host must not be able to spend a whole tick's budget and
        # then some.
        self.gh.candidates = [{"forge": forge.GITHUB, "repo": "o/r",
                               "number": n} for n in range(5)]
        self.gl.candidates = [{"forge": forge.GITLAB, "repo": "g/app",
                               "number": n} for n in range(5)]
        self.assertEqual(len(self.forges.reviewable_change_requests(3)), 3)


class TheInterfaceIsNotShapedAroundOneHost(unittest.TestCase):
    """Both implementations satisfy it — that is the acceptance criterion."""

    def test_neither_implementation_is_abstract(self):
        # A method added to the interface and forgotten in one implementation
        # makes that class un-instantiable, which is the failure we want:
        # loud, immediate, and at the point of the omission.
        forge.GitHubForge("t")
        forge.GitLabForge("https://g.invalid", "t")

    def test_every_question_is_answerable_on_both(self):
        names = sorted(
            n for n, v in vars(forge.Forge).items()
            if getattr(v, "__isabstractmethod__", False))
        self.assertIn("checks_state", names)
        self.assertIn("close_issue", names)
        for cls in (forge.GitHubForge, forge.GitLabForge):
            for name in names:
                with self.subTest(cls=cls.__name__, method=name):
                    self.assertIsNot(getattr(cls, name),
                                     getattr(forge.Forge, name),
                                     f"{cls.__name__} does not answer {name}")

    def test_close_intent_is_expressible_on_both(self):
        # The shape rule from the specification: GitLab has no `state_reason`,
        # so an interface that took one could not be satisfied there.
        import inspect
        for cls in (forge.GitHubForge, forge.GitLabForge):
            params = inspect.signature(cls.close_issue).parameters
            with self.subTest(cls=cls.__name__):
                self.assertIn("delivered", params)
                self.assertNotIn("state_reason", params)
                self.assertNotIn("reason", params)


if __name__ == "__main__":
    unittest.main()

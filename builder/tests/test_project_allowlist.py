"""The allowlist decides which repositories the bot may touch at all.

Its failure mode is asymmetric, which is why it gets this much attention. A
reference that fails to match is not a loud error — it is a repository
silently never worked on. A reference that matches when it should not is the
bot checking out, running an agent on, and pushing to somebody else's
repository, because all three subsystems discover their work from ACCOUNT-WIDE
queries: assign the bot an issue, request its review, add it as a
collaborator, and without this file it is at your service.

So every test here names the decision it protects, and the two that matter
most are the boring ones: an unreadable list permits NOTHING, and a redeploy
never re-grants what someone revoked.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from harness import BUILDER, TMP_ROOT, fake_path, load, load_script, temp_env

pa = load("project_allowlist")

CLI = os.path.join(BUILDER, "project-allow")

# The fake curl is a /bin/sh script; Windows cannot exec it, and the CLI would
# take its "could not ask" path instead of the one under test.
needs_curl = unittest.skipIf(os.name == "nt", "the fake curl needs a POSIX sh")


# Every test below except the GitLabProjects class describes the GITHUB
# ruleset, and which ruleset applies is a property of the environment: a
# deployment with GitLab credentials reads `a/b/c` as a nested project rather
# than as a malformed repository. So the environment that selects GitHub's
# rules is pinned here rather than inherited.
#
# Inheriting it was a real failure, not a hypothetical one: CI runners export
# GITLAB_URL as a masked variable, so these tests passed on every laptop and
# failed only in the pipeline, reporting a fault in the parser rather than in
# their own setup. Any developer with a GITLAB_API_TOKEN exported would have
# seen the same thing locally.
_SAVED_ENV: dict[str, str | None] = {}
_FORGE_ENV = ("GITLAB_URL", "GITLAB_HOST", "GITLAB_API_TOKEN", "GITLAB_TOKEN")


def setUpModule():
    for var in _FORGE_ENV:
        _SAVED_ENV[var] = os.environ.pop(var, None)


def tearDownModule():
    for var, was in _SAVED_ENV.items():
        if was is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = was



class Normalising(unittest.TestCase):
    def test_every_form_a_human_pastes_becomes_owner_repo(self):
        # All of these are things people actually send: the browser URL of the
        # repository, of an issue, of a pull request; both clone URLs; the
        # bare name. They are one repository and must be one permission state.
        want = "octocat/hello-world"
        for raw in (
            "octocat/hello-world",
            "  octocat/hello-world  ",
            '"octocat/hello-world"',
            "https://github.com/octocat/hello-world",
            "https://github.com/octocat/hello-world/",
            "https://github.com/octocat/hello-world.git",
            "https://www.github.com/octocat/hello-world",
            "HTTPS://GitHub.com/octocat/hello-world",
            "git@github.com:octocat/hello-world.git",
            "ssh://git@github.com/octocat/hello-world.git",
            "https://github.com/octocat/hello-world/issues/3",
            "https://github.com/octocat/hello-world/pull/7",
            "https://github.com/octocat/hello-world/tree/main",
            "https://github.com/octocat/hello-world/blob/main/README.md",
            "https://github.com/octocat/hello-world?tab=readme-ov-file",
            "https://github.com/octocat/hello-world#readme",
            "https://api.github.com/repos/octocat/hello-world",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(pa.normalize(raw), want)

    def test_an_en_dash_from_a_chat_client_still_matches(self):
        # Paste a name through a chat client or a wiki table and the hyphen can
        # come back as an en-dash. It would match nothing, forever, and look
        # identical in the reply — the worst possible bug for a permission file.
        self.assertEqual(pa.normalize("octocat/hello–world"),
                         "octocat/hello-world")

    def test_rejects_everything_that_is_not_a_repository(self):
        for raw in (
            "", "   ", "/", "//", "a//b",
            "hello-world",                     # no owner
            "3005",                            # an id says nothing to a human
            "-bad/name", "bad-/name",          # not legal owner names
            "octocat/", "octocat",
            "https://github.com/",
            "https://github.com/octocat",
            "octocat/hello-world/some-branch",  # not a known sub-resource
            "octocat/..", "octocat/.",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(pa.normalize(raw), "")

    def test_a_repository_on_another_host_is_not_this_repository(self):
        # The bot authenticates against one API. Quietly reducing a foreign URL
        # to owner/repo would permit a DIFFERENT repository that happens to
        # share the name — a grant the owner never made.
        for raw in (
            "https://example.com/octocat/hello-world",
            "git@git.example.org:octocat/hello-world.git",
            "https://api.example.com/repos/octocat/hello-world",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(pa.normalize(raw), "")

    def test_an_api_url_must_actually_be_a_repository_url(self):
        self.assertEqual(pa.normalize("https://api.github.com/user/repos"), "")

    def test_an_enterprise_host_can_be_configured(self):
        with temp_env(GITHUB_HOST="github.example.com"):
            self.assertEqual(
                pa.normalize("https://github.example.com/octocat/hello-world"),
                "octocat/hello-world")

    def test_dot_git_is_stripped_only_as_a_clone_suffix(self):
        # A repository may legitimately be named after a dotted domain or be
        # called "git"; stripping too eagerly would permit a name that does
        # not exist and silently work on nothing.
        self.assertEqual(pa.normalize("octocat/git"), "octocat/git")
        self.assertEqual(pa.normalize("octocat/octocat.github.io"),
                         "octocat/octocat.github.io")

    def test_case_is_preserved_but_not_significant(self):
        self.assertEqual(pa.normalize("Octocat/Hello-World"),
                         "Octocat/Hello-World")
        self.assertTrue(pa.Allowlist(["octocat/hello-world"])
                        .allows("Octocat/Hello-World"))



# GITLAB_URL is what makes a deployment GitLab-hosted, so every test in here
# sets it. Without it the module is GitHub-only, which is the subject of the
# last test in the class.
def gitlab():
    # A token as well as a URL: gitlab_hosts() treats a URL without one as no
    # GitLab at all, exactly as forge.configured() does.
    return temp_env(GITLAB_URL="https://gitlab.example.com",
                    GITLAB_HOST="https://gitlab.example.com",
                    GITLAB_API_TOKEN="glpat-test")


class GitLabProjects(unittest.TestCase):
    """A GitLab project must be grantable on a GitLab deployment.

    These exist because their absence was not theoretical: the allowlist was
    once narrowed to GitHub's exactly-two-segments while the planners stayed
    dual-forge, and on a GitLab deployment that parses every existing grant to
    nothing. The list then reports itself EMPTY rather than unreadable, so
    every subsystem idles quietly and `projects add` refuses the same path it
    is being asked to grant — a lockout with no way out from chat.
    """

    def test_a_path_with_namespace_is_a_project(self):
        with gitlab():
            self.assertEqual(pa.normalize("acme-corp/team/web-test"),
                             "acme-corp/team/web-test")

    def test_a_namespace_may_be_purely_numeric(self):
        # Top-level groups are routinely numeric (a cost centre, a department
        # code). A segment rule that assumed a leading letter would reject
        # every project under one, which is a whole organisation unable to
        # grant anything.
        with gitlab():
            self.assertEqual(pa.normalize("4711/team/web-test"),
                             "4711/team/web-test")

    def test_subgroups_nest_arbitrarily_deep(self):
        with gitlab():
            self.assertEqual(pa.normalize("acme-corp/team/sub/project"),
                             "acme-corp/team/sub/project")

    def test_the_forms_a_human_pastes_from_gitlab(self):
        with gitlab():
            for raw in (
                "https://gitlab.example.com/acme-corp/team/web-test",
                "https://gitlab.example.com/acme-corp/team/web-test/",
                "https://gitlab.example.com/acme-corp/team/web-test.git",
                # The web UI continues past the project with /-/...
                "https://gitlab.example.com/acme-corp/team/web-test/-/merge_requests/12",
                "https://gitlab.example.com/acme-corp/team/web-test/-/issues/3",
                "https://gitlab.example.com/acme-corp/team/web-test/-/tree/main",
                # Clone URLs. SSH terminates on a different host than the web
                # UI on this install, which is exactly what people copy.
                "git@ssh.gitlab.example.com:acme-corp/team/web-test.git",
                "https://gitlab.example.com/acme-corp/team/web-test.git",
            ):
                with self.subTest(raw=raw):
                    self.assertEqual(pa.normalize(raw),
                                     "acme-corp/team/web-test")

    def test_a_nested_path_is_taken_WHOLE_never_trimmed(self):
        # The GitHub ruleset trims a known subresource off owner/repo/<sub>.
        # Doing that here would read a project named `security` inside the
        # `acme-corp/team` subgroup as a grant of the ENTIRE subgroup — permitting
        # every project in it, none of which the owner named. Several of those
        # subresource words are ordinary project names.
        with gitlab():
            for name in ("security", "projects", "packages", "releases",
                         "tags", "wiki", "issues"):
                with self.subTest(name=name):
                    self.assertEqual(pa.normalize(f"acme-corp/team/{name}"),
                                     f"acme-corp/team/{name}")
                    self.assertFalse(
                        pa.Allowlist([f"acme-corp/team/{name}"]).allows("acme-corp/team"))

    def test_github_references_still_work_on_a_gitlab_deployment(self):
        # The planners are dual-forge; configuring one must not disable the
        # other, or a deployment that talks to both can only grant on one.
        with gitlab():
            for raw in ("octocat/hello-world",
                        "https://github.com/octocat/hello-world",
                        "https://github.com/octocat/hello-world/issues/5",
                        "git@github.com:octocat/hello-world.git"):
                with self.subTest(raw=raw):
                    self.assertEqual(pa.normalize(raw), "octocat/hello-world")

    def test_another_host_is_still_not_this_project(self):
        with gitlab():
            self.assertEqual(
                pa.normalize("https://gitlab.example.net/acme-corp/team/web-test"), "")
            self.assertEqual(
                pa.normalize("https://evil.example.com/octocat/hello-world"), "")

    def test_the_list_a_gitlab_deployment_actually_has_reads_back(self):
        with gitlab():
            allowed = pa.Allowlist(pa.parse(textwrap.dedent("""
                # Projects this bot is permitted to work on.
                acme-corp/team/web-test
                acme-corp/team/automation-test
            """)))
            self.assertEqual(len(allowed), 2)
            self.assertTrue(allowed.allows("acme-corp/team/web-test"))
            # Discovered but never granted: denied for the reason that says so.
            self.assertEqual(allowed.deny_reason("acme-corp/team/ungranted"), "not-permitted")

    def test_without_gitlab_configured_nothing_changes(self):
        # The GitHub-only deployment must behave exactly as it did before
        # GitLab was understood at all — a nested path grants nothing there.
        with temp_env(GITLAB_URL="", GITLAB_HOST="", GITLAB_API_TOKEN=""):
            self.assertEqual(pa.normalize("acme-corp/team/web-test"), "")
            self.assertEqual(pa.normalize("octocat/hello-world"),
                             "octocat/hello-world")


class ForgeSelection(unittest.TestCase):
    """The two rulesets, and the decision about which one applies.

    normalize() is the composition of these; they are exercised directly as
    well because the interesting cases are the ones where the two disagree,
    and a test that can only reach them through the composition cannot say
    which half was wrong.
    """

    def test_gitlab_hosts_needs_credentials_not_just_a_url(self):
        # A URL with no token is a host the bot cannot read. Treating it as
        # configured changes how every reference parses in exchange for a
        # grant that could never be acted on.
        with temp_env(GITLAB_URL="https://gitlab.example.com",
                      GITLAB_API_TOKEN=""):
            self.assertEqual(pa.gitlab_hosts(), set())
        with temp_env(GITLAB_URL="", GITLAB_API_TOKEN="glpat-test"):
            self.assertEqual(pa.gitlab_hosts(), set())

    def test_gitlab_hosts_covers_the_web_and_ssh_endpoints(self):
        with temp_env(GITLAB_URL="https://gitlab.example.com/",
                      GITLAB_API_TOKEN="glpat-test"):
            hosts = pa.gitlab_hosts()
        self.assertIn("gitlab.example.com", hosts)
        # Clone URLs commonly terminate somewhere other than the web host.
        self.assertIn("ssh.gitlab.example.com", hosts)
        self.assertIn("www.gitlab.example.com", hosts)

    def test_as_github_is_two_segments_and_trims_known_subresources(self):
        self.assertEqual(pa._as_github("octocat/hello-world"),
                         "octocat/hello-world")
        self.assertEqual(pa._as_github("octocat/hello-world/issues/5"),
                         "octocat/hello-world")
        # Not a subresource: an unrecognised third segment is a question, not
        # a guess at which repository was meant.
        self.assertEqual(pa._as_github("octocat/hello-world/some-branch"), "")
        self.assertEqual(pa._as_github("octocat"), "")

    def test_as_gitlab_nests_and_never_trims(self):
        self.assertEqual(pa._as_gitlab("acme-corp/team/web-test"),
                         "acme-corp/team/web-test")
        self.assertEqual(pa._as_gitlab("acme-corp/team/web-test.git"),
                         "acme-corp/team/web-test")
        self.assertEqual(
            pa._as_gitlab("acme-corp/team/web-test/-/merge_requests/12"),
            "acme-corp/team/web-test")
        # The GitHub ruleset would read this as a trim to `acme-corp/team`.
        self.assertEqual(pa._as_gitlab("acme-corp/team/issues"),
                         "acme-corp/team/issues")
        self.assertEqual(pa._as_gitlab("just-one-segment"), "")


class CliForgeRouting(unittest.TestCase):
    """Which forge the CLI verifies against, and which URL it prints back.

    In-process: none of these reach the network — the credential-less path
    returns before curl is invoked, and the lookup is given a stub — so they
    need the module rather than the subprocess sandbox CliTestCase builds.
    """

    def setUp(self):
        self.cli = load_script("project-allow")

    def test_is_gitlab_path_reads_nesting_then_credentials(self):
        with temp_env(GITLAB_URL="https://gitlab.example.com",
                      GITLAB_API_TOKEN="glpat-test", GITHUB_TOKEN="ghp-x"):
            # Nesting can only be GitLab, whatever else is configured.
            self.assertTrue(self.cli._is_gitlab_path("acme-corp/team/web-test"))
            # Two segments are a valid shape on both, so the forge that has
            # credentials decides — here GitHub also does, so it wins.
            self.assertFalse(self.cli._is_gitlab_path("octocat/hello-world"))
        with temp_env(GITLAB_URL="https://gitlab.example.com",
                      GITLAB_API_TOKEN="glpat-test", GITHUB_TOKEN="",
                      GH_TOKEN=""):
            self.assertTrue(self.cli._is_gitlab_path("octocat/hello-world"))
        with temp_env(GITLAB_URL="", GITLAB_API_TOKEN=""):
            self.assertFalse(self.cli._is_gitlab_path("acme-corp/team/web-test"))

    def test_gitlab_api_get_without_credentials_is_unverified_not_a_denial(self):
        # "We could not ask" must never be reported as "the project is not
        # there" — that would refuse a grant the owner is entitled to make.
        with temp_env(GITLAB_URL="https://gitlab.example.com",
                      GITLAB_API_TOKEN=""):
            data, err = self.cli._gitlab_api_get(
                "https://gitlab.example.com/api/v4/projects/x")
        self.assertEqual(data, {})
        self.assertEqual(err, "unverified")

    def test_gitlab_lookup_encodes_the_whole_path(self):
        # GitLab addresses a project by its percent-encoded full path; raw
        # slashes would read as a different endpoint entirely.
        seen = {}

        def fake_get(url):
            seen["url"] = url
            return ({"path_with_namespace": "acme-corp/team/web-test"}, "")

        with temp_env(GITLAB_URL="https://gitlab.example.com",
                      GITLAB_API_TOKEN="glpat-test"):
            with mock.patch.object(self.cli, "_gitlab_api_get", fake_get):
                canonical, err = self.cli._gitlab_lookup("acme-corp/team/web-test")
        self.assertEqual(err, "")
        self.assertEqual(canonical, "acme-corp/team/web-test")
        self.assertIn("acme-corp%2Fteam%2Fweb-test", seen["url"])
        self.assertNotIn("acme-corp/team/web-test", seen["url"])


class Parsing(unittest.TestCase):
    def test_ignores_comments_and_blanks(self):
        text = ("# the permitted repositories\n"
                "octocat/one\n"
                "\n"
                "   \n"
                "octocat/two   # granted for the release\n")
        self.assertEqual(pa.parse(text), ["octocat/one", "octocat/two"])

    def test_urls_in_the_file_are_understood(self):
        # Hand edits happen. A pasted URL that parsed to nothing would be a
        # grant the owner believes they made and the bot never sees.
        self.assertEqual(pa.parse("https://github.com/octocat/one\n"),
                         ["octocat/one"])

    def test_deduplicates_case_insensitively(self):
        self.assertEqual(
            pa.parse("octocat/one\nOctocat/One\noctocat/one\n"), ["octocat/one"])

    def test_a_line_that_cannot_be_understood_grants_nothing(self):
        # It must not take the rest of the file with it either: one bad line
        # revoking every other repository is a worse outcome than ignoring it.
        self.assertEqual(pa.parse("not a repo at all\noctocat/one\n"),
                         ["octocat/one"])


class Deciding(unittest.TestCase):
    def test_listed_is_allowed_and_unlisted_is_not(self):
        al = pa.Allowlist(["octocat/one"])
        self.assertTrue(al.allows("octocat/one"))
        self.assertIn("octocat/one", al)
        self.assertFalse(al.allows("octocat/two"))
        self.assertEqual(al.deny_reason("octocat/two"), "not-permitted")

    def test_an_unreadable_list_permits_NOTHING(self):
        # Fail closed. If we cannot tell what is permitted, the answer is not
        # "everything" — that is exactly how a bot ends up committing to a
        # repository nobody granted it.
        al = pa.Allowlist.denied()
        self.assertFalse(al.available)
        self.assertFalse(al.allows("octocat/one"))
        # A stable machine-readable code, not prose: the spawners match on it
        # to tell "could not read the list" from "not on the list", and those
        # two want different responses from an operator.
        self.assertEqual(al.deny_reason("octocat/one"), "allowlist-unavailable")
        self.assertNotEqual(al.deny_reason("octocat/one"),
                            pa.Allowlist([]).deny_reason("octocat/one"))

    def test_an_empty_but_READABLE_list_also_permits_nothing(self):
        al = pa.Allowlist([])
        self.assertTrue(al.available)
        self.assertFalse(al.allows("octocat/one"))
        self.assertEqual(al.deny_reason("octocat/one"), "allowlist-empty")

    def test_a_url_is_matched_against_a_stored_name(self):
        al = pa.Allowlist(["octocat/one"])
        self.assertTrue(al.allows("https://github.com/octocat/one/issues/4"))

    def test_junk_is_never_allowed_even_by_a_populated_list(self):
        al = pa.Allowlist(["octocat/one"])
        self.assertFalse(al.allows(""))
        self.assertFalse(al.allows("octocat"))


class ReadingFromDisk(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.home = tempfile.mkdtemp(prefix="allowlist-", dir=TMP_ROOT)
        os.makedirs(os.path.join(self.home, ".openclaw"))
        self.list_file = os.path.join(self.home, ".openclaw",
                                      "projects-allowed.list")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_a_missing_list_is_empty_but_READ(self):
        # Nothing has been permitted yet — an ordinary first-boot state, not a
        # fault. It still permits nothing.
        with temp_env(HOME=self.home):
            al = pa.read_local()
        self.assertTrue(al.available)
        self.assertEqual(al.entries, [])
        self.assertFalse(al.allows("octocat/one"))

    def test_a_list_that_cannot_be_read_permits_NOTHING(self):
        # A directory where the file should be stands in for every way a read
        # fails that is not "absent" — a mangled mount, a permission change, a
        # half-restored PVC. All of them must deny.
        os.makedirs(self.list_file)
        with temp_env(HOME=self.home):
            al = pa.read_local()
        self.assertFalse(al.available)
        self.assertFalse(al.allows("octocat/one"))
        self.assertEqual(al.deny_reason("octocat/one"), "allowlist-unavailable")

    def test_a_normal_list_reads_back(self):
        with open(self.list_file, "w", encoding="utf-8") as f:
            f.write("# header\noctocat/one\nhttps://github.com/octocat/two\n")
        with temp_env(HOME=self.home):
            al = pa.read_local()
        self.assertEqual(al.entries, ["octocat/one", "octocat/two"])


class PodSection(unittest.TestCase):
    def test_snippet_emits_its_marker(self):
        self.assertIn(pa.ALLOWED_MARKER, pa.pod_read_snippet())
        self.assertIn(pa.LIST_REL, pa.pod_read_snippet())

    def test_section_round_trips(self):
        al = pa.from_section(["octocat/one", "octocat/two"])
        self.assertTrue(al.available)
        self.assertEqual(len(al), 2)

    def test_a_failed_exec_is_unavailable_not_empty(self):
        # An exec that failed produces no lines. Reading that as "zero
        # permitted repositories" would be indistinguishable from a real empty
        # list, and only one of those two is a fault someone must fix.
        al = pa.from_section([], available=False)
        self.assertFalse(al.available)
        self.assertEqual(al.deny_reason("octocat/one"), "allowlist-unavailable")


class CliTestCase(unittest.TestCase):
    """Runs `project-allow` as its own process, in a throwaway HOME, with the
    fake curl first on PATH — the same way the chat skill and the init
    container call it."""

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.home = tempfile.mkdtemp(prefix="allowcli-", dir=TMP_ROOT)
        self.state = os.path.join(self.home, ".openclaw")
        os.makedirs(self.state)
        self.list_file = os.path.join(self.state, "projects-allowed.list")
        self.log_file = os.path.join(self.state, "projects-allowed.log")
        self.fixtures = os.path.join(self.home, "fixtures")
        os.makedirs(self.fixtures)
        self.curl_log = os.path.join(self.home, "curl.log")
        # A clean environment: an inherited token or bootstrap seed from the
        # developer's shell would quietly change what these tests exercise.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GITHUB_", "GH_", "PROJECT"))}
        self.env.update({
            "HOME": self.home,
            # Copied into the sandbox rather than pointed at in the
            # checkout: a fake is only a fake if the shell can execute it,
            # and the execute bit in a checkout is not something a test
            # should depend on.
            "PATH": (fake_path(os.path.join(self.home, "bin"))
                     + os.pathsep + os.environ.get("PATH", "")),
            "FAKE_CURL_LOG": self.curl_log,
        })

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def cli(self, *args, **env):
        e = dict(self.env)
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run([sys.executable, CLI, *args],
                              capture_output=True, text=True, env=e, timeout=60)

    def serve(self, repo: str, full_name: str | None = None) -> None:
        """Make the API answer 200 for this repository. Anything without a
        fixture answers like a 404, which is what `add` must refuse."""
        slug = "repos_" + re.sub(r"[/.]", "_", repo)
        with open(os.path.join(self.fixtures, slug), "w", encoding="utf-8") as f:
            json.dump({"full_name": full_name or repo, "private": False}, f)

    def api_env(self, token: str = "t0ken") -> dict:
        return {"GITHUB_TOKEN": token, "FAKE_CURL_DIR": self.fixtures}

    def requests(self) -> list[str]:
        if not os.path.exists(self.curl_log):
            return []
        with open(self.curl_log, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]

    def body(self) -> str:
        if not os.path.exists(self.list_file):
            return ""
        with open(self.list_file, encoding="utf-8") as f:
            return f.read()

    def entries(self) -> list[str]:
        return pa.parse(self.body())

    def audit(self) -> list[list[str]]:
        if not os.path.exists(self.log_file):
            return []
        with open(self.log_file, encoding="utf-8") as f:
            return [line.rstrip("\n").split("\t") for line in f if line.strip()]


class Checking(CliTestCase):
    """`check` is the guard every runner calls before it touches a repository,
    and the spawners call on every five-minute tick."""

    def test_a_missing_list_denies(self):
        p = self.cli("check", "octocat/one")
        self.assertEqual(p.returncode, 2)
        self.assertIn("allowlist-empty", p.stderr)

    def test_an_unreadable_list_denies_with_its_own_reason(self):
        os.makedirs(self.list_file)
        p = self.cli("check", "octocat/one")
        self.assertEqual(p.returncode, 2)
        self.assertIn("allowlist-unavailable", p.stderr)

    def test_a_permitted_repository_passes(self):
        self.cli("add", "octocat/one", "--force")
        self.assertEqual(self.cli("check", "octocat/one").returncode, 0)
        self.assertEqual(
            self.cli("check", "https://github.com/octocat/one/pull/9").returncode,
            0)

    def test_an_unpermitted_repository_is_refused(self):
        self.cli("add", "octocat/one", "--force")
        p = self.cli("check", "stranger/thing")
        self.assertEqual(p.returncode, 2)
        self.assertIn("not-permitted", p.stderr)

    @needs_curl
    def test_check_never_touches_the_network(self):
        # It runs on every tick and inside every runner. If a GitHub outage
        # could turn into a permission decision, the outage would stop the bot
        # working on repositories that ARE permitted — or worse, be papered
        # over with a fallback.
        self.cli("add", "octocat/one", "--force")
        if os.path.exists(self.curl_log):
            os.unlink(self.curl_log)
        self.cli("check", "octocat/one", **self.api_env())
        self.assertEqual(self.requests(), [])


class Granting(CliTestCase):
    @needs_curl
    def test_add_verifies_the_repository_over_the_api(self):
        self.serve("octocat/hello-world")
        p = self.cli("add", "https://github.com/octocat/hello-world/issues/3",
                     "--actor", "owner", **self.api_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.entries(), ["octocat/hello-world"])
        self.assertEqual(
            self.requests(),
            ["GET https://api.github.com/repos/octocat/hello-world"])

    @needs_curl
    def test_the_canonical_name_from_the_api_is_what_gets_stored(self):
        # Two spellings of one repository must not become two permission
        # states, and the list is read by humans.
        self.serve("octocat/hello-world", full_name="Octocat/Hello-World")
        p = self.cli("add", "octocat/hello-world", **self.api_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.entries(), ["Octocat/Hello-World"])

    @needs_curl
    def test_a_repository_the_api_does_not_know_is_refused_with_code_3(self):
        p = self.cli("add", "stranger/private-thing", **self.api_env())
        self.assertEqual(p.returncode, 3)
        self.assertEqual(self.entries(), [])
        self.assertFalse(os.path.exists(self.list_file))
        self.assertEqual(self.audit(), [])
        self.assertIn("--force", p.stderr)

    @needs_curl
    def test_force_grants_anyway(self):
        # A brand new repository, or one the token cannot see yet. The owner
        # is allowed to insist; the point is that they have to.
        p = self.cli("add", "stranger/private-thing", "--force",
                     **self.api_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.entries(), ["stranger/private-thing"])

    def test_an_api_we_cannot_reach_does_not_block_a_grant(self):
        # No token, no curl, no network: we could not ask. Refusing here would
        # make an offline cluster unable to grant permission at all, which is
        # a different failure from letting an unverified name in — and the
        # output says which one happened.
        p = self.cli("add", "octocat/one")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("unverified", p.stdout)
        self.assertEqual(self.entries(), ["octocat/one"])

    def test_input_that_is_not_a_repository_is_refused_before_anything_is_written(self):
        for raw in ("3005", "hello-world", "octocat/one/tree-ish/deep"):
            with self.subTest(raw=raw):
                p = self.cli("add", raw)
                self.assertEqual(p.returncode, 1)
                self.assertFalse(os.path.exists(self.list_file))
                self.assertEqual(self.audit(), [])

    def test_adding_the_same_repository_twice_changes_nothing(self):
        self.cli("add", "octocat/one", "--force")
        before = self.body()
        p = self.cli("add", "https://github.com/octocat/one.git", "--force")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.body(), before)
        self.assertEqual(len(self.audit()), 1)

    def test_add_refuses_to_overwrite_a_list_it_could_not_read(self):
        # Writing here would replace an unknown permission set with a guess.
        os.makedirs(self.list_file)
        p = self.cli("add", "octocat/one", "--force")
        self.assertEqual(p.returncode, 2)
        self.assertTrue(os.path.isdir(self.list_file))

    def test_list_prints_urls_paths_and_json(self):
        self.cli("add", "octocat/one", "--force")
        self.assertIn("https://github.com/octocat/one", self.cli("list").stdout)
        self.assertEqual(self.cli("list", "--paths").stdout.strip(),
                         "octocat/one")
        data = json.loads(self.cli("list", "--json").stdout)
        self.assertTrue(data["available"])
        self.assertEqual(data["repos"], ["octocat/one"])
        self.assertEqual(data["count"], 1)

    def test_list_reports_an_unreadable_list_as_unavailable(self):
        os.makedirs(self.list_file)
        p = self.cli("list", "--json")
        self.assertEqual(p.returncode, 2)
        self.assertFalse(json.loads(p.stdout)["available"])
        self.assertEqual(self.cli("list").returncode, 2)


class Revoking(CliTestCase):
    def test_revoke_removes_permission(self):
        self.cli("add", "octocat/one", "octocat/two", "--force")
        p = self.cli("revoke", "https://github.com/octocat/one", "--actor",
                     "owner")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.entries(), ["octocat/two"])
        self.assertEqual(self.cli("check", "octocat/one").returncode, 2)

    def test_revoking_something_that_was_never_permitted_writes_nothing(self):
        self.cli("add", "octocat/one", "--force")
        before = self.body()
        audit_before = len(self.audit())
        p = self.cli("revoke", "stranger/thing")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.body(), before)
        self.assertEqual(len(self.audit()), audit_before)

    def test_revoke_says_that_a_run_in_flight_is_not_killed(self):
        # A revoke that reads as "stopped immediately" when it did not is how
        # an owner walks away from a bot still working on their repository.
        self.cli("add", "octocat/one", "--force")
        p = self.cli("revoke", "octocat/one")
        self.assertIn("in flight", p.stdout)


class AtomicWrite(unittest.TestCase):
    """Three spawners and every runner read this file on a five-minute tick
    nobody coordinates with. A truncated read is a permission set nobody
    chose."""

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.home = tempfile.mkdtemp(prefix="allowatomic-", dir=TMP_ROOT)
        self.state = os.path.join(self.home, ".openclaw")
        os.makedirs(self.state)
        self.list_file = os.path.join(self.state, "projects-allowed.list")
        self.cli = load_script("project-allow")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _siblings(self):
        return [f for f in os.listdir(self.state)
                if f != "projects-allowed.list" and f != "projects-allowed.log"]

    def _body(self):
        with open(self.list_file, encoding="utf-8") as f:
            return f.read()

    def test_a_reader_mid_write_sees_the_old_list_never_a_partial_one(self):
        with temp_env(HOME=self.home):
            self.cli._write(["octocat/one"])
            seen = {}
            real_replace = os.replace

            def spy(src, dst):
                # The instant before the swap — the worst moment for a tick to
                # read. Whatever it gets must be a COMPLETE list.
                seen["mid"] = pa.read_local().entries
                return real_replace(src, dst)

            with mock.patch("os.replace", spy):
                self.cli._write(["octocat/one", "octocat/two"])
            self.assertEqual(seen["mid"], ["octocat/one"])
            self.assertEqual(pa.read_local().entries,
                             ["octocat/one", "octocat/two"])

    def test_a_failed_write_leaves_the_previous_list_intact(self):
        with temp_env(HOME=self.home):
            self.cli._write(["octocat/one"])
            before = self._body()
            with mock.patch("os.replace", side_effect=OSError(28, "no space")):
                with self.assertRaises(OSError):
                    self.cli._write(["octocat/one", "octocat/two"])
            self.assertEqual(self._body(), before)
        # And no temp file left beside a permission list for someone to
        # "restore" later.
        self.assertEqual(self._siblings(), [])

    def test_a_successful_write_leaves_no_temporary_file(self):
        with temp_env(HOME=self.home):
            self.cli._write(["octocat/one"])
        self.assertEqual(self._siblings(), [])

    def test_the_written_file_explains_itself_and_reads_back(self):
        with temp_env(HOME=self.home):
            self.cli._write(["octocat/one", "octocat/two"])
            self.assertEqual(pa.read_local().entries,
                             ["octocat/one", "octocat/two"])
        body = self._body()
        self.assertTrue(body.startswith("#"))
        self.assertIn("project-allow", body)


class Bootstrapping(CliTestCase):
    """The init container seeds the list on first boot. Everything about this
    command is about what it must NOT do on the second."""

    def test_seeds_from_the_environment_on_first_boot(self):
        p = self.cli("bootstrap",
                     PROJECTS_ALLOWED_BOOTSTRAP=(
                         "octocat/one, https://github.com/octocat/two"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.entries(), ["octocat/one", "octocat/two"])

    def test_a_second_boot_never_overwrites_the_list(self):
        self.cli("bootstrap", PROJECTS_ALLOWED_BOOTSTRAP="octocat/one")
        self.cli("add", "octocat/added-later", "--force")
        p = self.cli("bootstrap", PROJECTS_ALLOWED_BOOTSTRAP="octocat/one")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.entries(), ["octocat/one", "octocat/added-later"])
        self.assertIn("left unchanged", p.stdout)

    def test_a_redeploy_NEVER_undoes_a_REVOKE(self):
        # The reason this command exists in this shape. The seed env lives in
        # the Deployment, so it still names the revoked repository on the next
        # rollout; re-seeding would hand back permission the owner
        # deliberately took away, and nothing would say so.
        self.cli("bootstrap", PROJECTS_ALLOWED_BOOTSTRAP="octocat/one,octocat/two")
        self.cli("revoke", "octocat/two", "--actor", "owner")
        self.assertEqual(self.entries(), ["octocat/one"])
        self.cli("bootstrap", PROJECTS_ALLOWED_BOOTSTRAP="octocat/one,octocat/two")
        self.assertEqual(self.entries(), ["octocat/one"])
        self.assertEqual(self.cli("check", "octocat/two").returncode, 2)

    def test_an_emptied_list_stays_empty(self):
        # "Everything revoked" and "never seeded" look the same on disk apart
        # from the file existing. That difference is the whole guard.
        self.cli("bootstrap", PROJECTS_ALLOWED_BOOTSTRAP="octocat/one")
        self.cli("revoke", "octocat/one")
        self.assertEqual(self.entries(), [])
        self.cli("bootstrap", PROJECTS_ALLOWED_BOOTSTRAP="octocat/one")
        self.assertEqual(self.entries(), [])

    def test_no_seed_still_creates_a_readable_empty_list(self):
        p = self.cli("bootstrap")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(self.list_file))
        self.assertEqual(self.entries(), [])
        # Readable and empty — so the planners report "nothing permitted"
        # rather than "cannot read the list".
        self.assertEqual(self.cli("check", "octocat/one").returncode, 2)
        self.assertIn("allowlist-empty",
                      self.cli("check", "octocat/one").stderr)

    def test_junk_in_the_seed_does_not_become_a_grant(self):
        self.cli("bootstrap",
                 PROJECTS_ALLOWED_BOOTSTRAP="octocat/one,,3005,nonsense")
        self.assertEqual(self.entries(), ["octocat/one"])

    @needs_curl
    def test_bootstrap_asks_the_api_for_nothing(self):
        # It runs in an init container, before the pod is necessarily able to
        # reach anything. A network dependency here would gate the whole
        # deployment on GitHub being up.
        self.cli("bootstrap", PROJECTS_ALLOWED_BOOTSTRAP="octocat/one",
                 **self.api_env())
        self.assertEqual(self.requests(), [])

    def test_an_unreadable_existing_list_is_left_alone(self):
        os.makedirs(self.list_file)
        p = self.cli("bootstrap", PROJECTS_ALLOWED_BOOTSTRAP="octocat/one")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(os.path.isdir(self.list_file))


class AuditTrail(CliTestCase):
    """Every grant and revocation is recorded. An unexplained entry in a
    permission list is its own kind of incident."""

    def test_a_grant_records_timestamp_action_repo_and_actor(self):
        self.cli("add", "octocat/one", "--force", "--actor", "konstantin")
        rows = self.audit()
        self.assertEqual(len(rows), 1)
        stamp, action, repo, actor = rows[0]
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual([action, repo, actor], ["add", "octocat/one",
                                                 "konstantin"])

    def test_a_revocation_is_recorded_too(self):
        self.cli("add", "octocat/one", "--force", "--actor", "konstantin")
        self.cli("revoke", "octocat/one", "--actor", "konstantin")
        self.assertEqual([r[1] for r in self.audit()], ["add", "revoke"])

    def test_the_log_is_append_only_across_operations(self):
        self.cli("add", "octocat/one", "--force", "--actor", "a")
        self.cli("add", "octocat/two", "--force", "--actor", "b")
        self.cli("revoke", "octocat/one", "--actor", "c")
        rows = self.audit()
        self.assertEqual([r[1] for r in rows], ["add", "add", "revoke"])
        self.assertEqual([r[2] for r in rows],
                         ["octocat/one", "octocat/two", "octocat/one"])
        self.assertEqual([r[3] for r in rows], ["a", "b", "c"])

    def test_an_unnamed_actor_still_produces_a_complete_line(self):
        self.cli("add", "octocat/one", "--force")
        self.assertEqual(len(self.audit()[0]), 4)
        self.assertTrue(self.audit()[0][3])

    def test_the_seed_is_attributed_to_the_deployment(self):
        self.cli("bootstrap", PROJECTS_ALLOWED_BOOTSTRAP="octocat/one")
        self.assertEqual(self.audit()[0][1:], ["bootstrap", "octocat/one",
                                               "deploy"])


if __name__ == "__main__":
    unittest.main()

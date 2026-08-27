"""Nobody may turn a repository path into a person, ever again.

WHAT HAPPENED, TWICE
--------------------
`group/team/app` is how a project is addressed on a hosted GitLab. The
first segment is a GROUP — a department, forty-four members, six of them
Owners by inheritance. Two different files took that segment to be "the owner":

  * fixer-runner.sh   `repo_owner_login() { echo "${REPO%%/*}"; }`
  * heartbeat-issue-tick.py   `mention = repo.split("/", 1)[0]`

The first was found and fixed. The second posted the destructive-change
confirmation question — "@group — I need clarification before writing any code"
— to the entire department, on two issues, minutes after the first fix
shipped. A group @-mention notifies every member; assigning one assigns every
member.

WHY THIS FILE AND NOT ONLY THE UNIT TESTS
-----------------------------------------
The unit tests pin what each resolver ANSWERS. They cannot see a third file
that never asks a resolver at all, which is exactly how the second one
survived the first fix. This reads the shipped source instead and fails on the
SHAPE of the mistake, wherever it appears.

Three rules, and each is a thing that actually went wrong:

  1. Nothing shipped may derive a person from a repository path.
  2. Every @-mention the bot writes comes from a resolver, not from a
     path fragment.
  3. The hosts defuse a mention that is not one human, so a fourth file
     making the same mistake still cannot page a department.
"""

import os
import re
import unittest

from harness import BUILDER, load  # noqa: F401 - puts builder/ on sys.path

import forge  # noqa: E402


# `${REPO%%/*}` in shell, `repo.split("/")[0]` in python: the first path
# segment, harvested as if it were an account.
_SHELL_SPLIT = re.compile(r"\$\{REPO%%/\*\}")
_PY_SPLIT = re.compile(r"\brepo\s*\.\s*(?:split|partition)\s*\(\s*[\"']/[\"']")

# Files that are allowed to talk about the mistake without making it: this
# test, and the comments in the two files that were fixed.
def _lines(path):
    # errors="ignore": a couple of shipped files are not text at all, and a
    # scanner that dies on one silently stops scanning the rest.
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for n, line in enumerate(fh, 1):
            stripped = line.lstrip()
            # A comment may NAME the pattern — that is how the fix explains
            # itself. Only live code counts.
            if stripped.startswith("#"):
                continue
            yield n, line


def _shipped():
    """Every file that runs on the bot. Not the tests, not the tooling."""
    for entry in sorted(os.listdir(BUILDER)):
        path = os.path.join(BUILDER, entry)
        if os.path.isdir(path) or entry.endswith((".md", ".json", ".txt",
                                                  ".env", ".conf")):
            continue
        yield entry, path


class NoBodyTurnsAPathIntoAPerson(unittest.TestCase):
    def test_no_shipped_file_harvests_the_first_path_segment(self):
        offenders = []
        for name, path in _shipped():
            if name == "project_allowlist.py":
                # It splits paths to VALIDATE them — `owner/repo` is a shape
                # there, never somebody to notify. It writes no comments.
                continue
            if name.startswith("forge_"):
                # The hosts split a path to ADDRESS a project (`/repos/o/n`).
                # What they must not do is call the result a person, and
                # owner_login is tested for exactly that.
                continue
            for n, line in _lines(path):
                if _SHELL_SPLIT.search(line) or _PY_SPLIT.search(line):
                    offenders.append(f"{name}:{n}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "a repository path is being read as an account — the first "
            "segment is a GROUP on GitLab and an ORGANISATION on GitHub. Ask "
            "the host: forge-cli owner / Forge.owner_login.\n  "
            + "\n  ".join(offenders))


class EveryHostRefusesToPageACrowd(unittest.TestCase):
    """The last line of defence, asked of the hosts that can page one."""

    class _Transport:
        """Answers the user lookup: `ada` is a person, nothing else is."""

        def __init__(self):
            self.wrote = []

        def __call__(self, method, url, *, headers, params=None,
                     json_body=None, form_body=None, timeout=None, raw=False):
            if method == "GET" and "/orgs/" in url:
                # Gitea asks this FIRST: the endpoint knows only organisations,
                # so an answer here means "not a person".
                name = url.rsplit("/", 1)[-1]
                return {"id": 9, "username": name} if name == "acme" else None
            if method == "GET" and "/users" in url:
                # GitLab: /users?username=x   GitHub and Gitea: /users/x
                name = (params or {}).get("username") or url.rsplit("/", 1)[-1]
                if name == "ada":
                    return ([{"id": 1, "username": "ada"}]
                            if params else {"id": 1, "login": "ada",
                                            "type": "User"})
                return [] if params else {"login": name,
                                          "type": "Organization"}
            self.wrote.append(json_body or form_body or {})
            return {"iid": 7, "number": 7, "id": 7}

    def bodies(self, impl, t):
        impl.post_comment("group/team/app", 5, "@group — please confirm. cc @ada")
        return [w.get("body", "") for w in t.wrote]

    def test_gitlab_defuses_a_group_and_keeps_the_person(self):
        t = self._Transport()
        impl = forge.GitLabForge("https://gl.invalid", "tok", transport=t)
        body = self.bodies(impl, t)[0]
        self.assertNotIn("@group", body, "a department was just notified")
        self.assertIn("`group`", body, "the sentence lost what it was about")
        self.assertIn("@ada", body, "a real person stopped being notified")

    def test_github_defuses_a_team_and_keeps_the_person(self):
        t = self._Transport()
        impl = forge.GitHubForge("tok", transport=t)
        impl.post_comment("acme/app", 5, "@acme/platform — please confirm. "
                                         "cc @ada")
        body = [w.get("body", "") for w in t.wrote][0]
        self.assertNotIn("@acme/platform", body, "a team was just notified")
        self.assertIn("@ada", body)

    def test_gitea_defuses_an_organisation_and_keeps_the_person(self):
        # An organisation is an account on this host, so the path alone cannot
        # tell the two apart — the org endpoint is what answers.
        t = self._Transport()
        impl = forge.GiteaForge("https://gitea.invalid", "tok", transport=t)
        impl.post_comment("acme/app", 5, "@acme — please confirm. cc @ada")
        # The org probe is a call too, so take the write that carries prose.
        body = [w.get("body", "") for w in t.wrote if w.get("body")][0]
        self.assertNotIn("@acme", body, "an organisation was just notified")
        self.assertIn("@ada", body)

    def test_azure_devops_leaves_its_inert_text_alone(self):
        # A mention there is markup carrying an identity GUID; plain "@name"
        # notifies nobody, so defusing it would only mangle the sentence.
        impl = forge.AzureDevOpsForge("https://dev.azure.invalid/acme", "tok",
                                      transport=self._Transport())
        self.assertEqual(impl._one_human_only("@anybody at all"),
                         "@anybody at all")

    def test_every_host_answers_the_question_at_all(self):
        """The guard is on the INTERFACE, so a fifth host inherits it.

        Both copies of this bug were a file that did its own thing. One
        implementation on the base class is the point.
        """
        for name in ("_one_human_only", "_is_user", "_mention_seen"):
            self.assertTrue(hasattr(forge.Forge, name), name)

    def test_the_guard_is_named_where_it_lives(self):
        """`_one_human_only` and `_is_user` are the chokepoint, per host.

        Named here so the function ledger records them as covered, and so a
        rename has to come past this file — the guard being quietly dropped is
        the failure it is here to prevent.
        """
        for impl in (forge.GitLabForge("https://gl.invalid", "t",
                                       transport=self._Transport()),
                     forge.GitHubForge("t", transport=self._Transport())):
            self.assertTrue(impl._is_user("ada"))
            self.assertFalse(impl._is_user("group"))
            self.assertEqual(impl._one_human_only("plain text"), "plain text")

    def test_a_mention_inside_code_is_left_alone(self):
        t = self._Transport()
        impl = forge.GitLabForge("https://gl.invalid", "tok", transport=t)
        impl.post_comment("group/team/app", 5, "the literal `@group` in a span")
        self.assertIn("`@group`", [w.get("body", "") for w in t.wrote][0])

    def test_every_prose_write_passes_the_guard(self):
        """Not just issue comments: a review body is prose the bot wrote too.

        Each of these is a place a sentence reaches people. One of them left
        unguarded is the whole gap back.
        """
        t = self._Transport()
        gl = forge.GitLabForge("https://gl.invalid", "tok", transport=t)
        gl.post_change_request_comment("group/team/app", 3, "@group look")
        gl.submit_review("group/team/app", 3, "comment", "@group look")
        gl.create_issue("group/team/app", "t", "@group look", None, ["ada"])
        gh_t = self._Transport()
        gh = forge.GitHubForge("tok", transport=gh_t)
        gh.post_change_request_comment("acme/app", 3, "@acme/platform look")
        gh.submit_review("acme/app", 3, "comment", "@acme/platform look")
        gh.create_issue("acme/app", "t", "@acme/platform look", None, ["ada"])
        for w in t.wrote + gh_t.wrote:
            text = str(w.get("body") or w.get("description") or "")
            if not text:
                continue
            self.assertNotIn("@group", text, w)
            self.assertNotIn("@acme/platform", text, w)

    def test_only_one_reviewer_is_ever_requested(self):
        t = self._Transport()
        gh = forge.GitHubForge("tok", transport=t)
        gh.request_review("acme/app", 3, ["ada", "bob", "carol"])
        self.assertEqual(t.wrote[-1].get("reviewers"), ["ada"])
        gt_t = self._Transport()
        gt = forge.GiteaForge("https://gitea.invalid", "tok", transport=gt_t)
        gt.request_review("acme/app", 3, ["ada", "bob", "carol"])
        self.assertEqual(gt_t.wrote[-1].get("reviewers"), ["ada"])
        gl_t = self._Transport()
        gl = forge.GitLabForge("https://gl.invalid", "tok", transport=gl_t)
        gl.request_review("group/team/app", 3, ["ada"])
        self.assertEqual(gl_t.wrote[-1].get("reviewer_ids"), "1")

    def test_only_one_account_is_ever_assigned(self):
        for label, impl in (
                ("gitlab", forge.GitLabForge("https://gl.invalid", "tok",
                                             transport=self._Transport())),
                ("github", forge.GitHubForge("tok",
                                             transport=self._Transport()))):
            with self.subTest(host=label):
                t = impl._transport
                impl.create_issue("group/team/app", "t", "b", None,
                                  ["ada", "bob", "carol"])
                sent = t.wrote[-1]
                got = sent.get("assignee_ids") or sent.get("assignees")
                self.assertIn(str(got), ("1", "['ada']"), f"{label}: {sent}")


if __name__ == "__main__":
    unittest.main()

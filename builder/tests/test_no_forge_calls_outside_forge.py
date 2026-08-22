"""Nothing outside the forge implementations may know a code host's URLs.

WHY A GREP IS THE RIGHT TEST HERE
---------------------------------
The seam is only worth having while it holds. Every decision this bot makes
used to be written in the same expression as the request that answered it, and
the reason that was expensive is that it was invisible: a new `curl` in a
runner, or one more `f"{API}/repos/..."` in a planner, reads as perfectly
ordinary code and quietly puts a second host out of reach again.

So the rule is checked mechanically rather than remembered. A file that names
a code host's API, or spells one of its REST paths, has transport in it — and
transport belongs in forge.py.

THE ALLOWLIST IS THE WORK THAT IS NOT DONE YET, and it is written out one file
at a time on purpose. Each entry names what still has to be converted and why
it has not been. When a file is converted its entry is deleted and the check
covers it from then on — which is the point of listing them rather than
excluding a directory.
"""

import os
import re
import unittest

BUILDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A code host's API, spelled any of the ways this repository has spelled it.
HOSTS = re.compile(
    r"api\.github\.com"
    r"|raw\.githubusercontent\.com"
    r"|/api/graphql"
    r"|/api/v4\b",
    re.IGNORECASE,
)

# A REST path belonging to one of those APIs. Anchored on the collection names
# so an ordinary directory called `projects/` or a sentence about issues does
# not trip it.
PATHS = re.compile(
    r"/repos/[^\s\"']*/(issues|pulls|commits|branches|labels|merges"
    r"|code-scanning|git/refs|contents|actions)"
    r"|/search/issues"
    r"|/user/repos\b"
    r"|/projects/[^\s\"']*/(issues|merge_requests|pipelines|repository"
    r"|jobs|labels|members|uploads)",
)

# Files that legitimately hold transport: the implementations themselves, and
# the tests that pin them.
#
# The seam and the transport stayed in `forge.py`; one module per host sits
# flat beside it. Listed by name rather than matched by a `forge_*` glob on
# purpose — a new file that speaks REST should be a deliberate entry here,
# which is the guarantee the single filename used to give.
IMPLEMENTATIONS = {"forge.py", "forge_github.py", "forge_gitlab.py",
                   "forge_gitea.py", "forge_azdo.py", "forge-cli"}

# ---------------------------------------------------------------------------
# NOT CONVERTED YET. Delete an entry when its file stops speaking REST — the
# check then covers it automatically, and a regression puts it back on the
# list only by failing this test.
# ---------------------------------------------------------------------------
ALLOWED = {
    # The remaining subsystems, each of which reaches a host for one narrow
    # purpose and none of which a planner depends on. The three RUNNERS are no
    # longer among them: they cannot import this module, so they shell out to
    # `forge-cli`, which is this module — one implementation, not a second one
    # written in bash that would drift from it in silence.
    "planning",                 # records what was delivered, onto the issue
    "project-allow",            # validates a repository the owner names
    "project-instructions.sh",  # fetches a project's own instructions file
    "record-deliveries",        # walks merged work to build the delivery log
    "security_reports.py",      # posts a scan result onto a change request
    "tester-upload-screenshots.py",   # commits run artefacts to a branch

    # Not transport at all, and never converted: a list of the hostnames a
    # repository URL may name, used to refuse a project on a host this bot
    # does not talk to.
    "project_allowlist.py",
    # Likewise: a comment describing the request the TESTER makes, in the file
    # that says so explicitly ("this file never makes a request of its own").
    "project-kind.sh",
}

# Directories with no source of ours in them.
SKIP_DIRS = {"__pycache__", "tests", "node_modules", ".git"}


def _sources():
    """Every file under builder/ that could contain a request."""
    for root, dirs, files in os.walk(BUILDER):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                   and not d.endswith("-mcp")]
        for name in files:
            path = os.path.join(root, name)
            if os.path.getsize(path) > 2_000_000:
                continue          # a vendored binary, not our source
            yield os.path.relpath(path, BUILDER).replace(os.sep, "/")


def _offends(rel: str) -> str:
    with open(os.path.join(BUILDER, rel), "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="ignore")
    for line in text.splitlines():
        if HOSTS.search(line) or PATHS.search(line):
            return line.strip()
    return ""


class NoTransportOutsideTheForge(unittest.TestCase):
    maxDiff = None

    def test_the_only_files_that_speak_rest_are_the_ones_on_the_list(self):
        offenders = {}
        for rel in _sources():
            if rel in IMPLEMENTATIONS or rel in ALLOWED:
                continue
            line = _offends(rel)
            if line:
                offenders[rel] = line
        self.assertEqual(offenders, {}, (
            "these files name a code host's API or one of its REST paths. "
            "Ask the forge instead — see forge.py. If the file genuinely "
            "cannot yet, add it to ALLOWED with a comment saying why."))

    def test_the_three_planners_are_clean(self):
        # Stated separately from the sweep above so the failure names the
        # thing that regressed rather than a set difference.
        for planner in ("heartbeat-issue-tick.py", "reviewer-tick.py",
                        "tester-tick.py"):
            with self.subTest(planner=planner):
                self.assertEqual(_offends(planner), "")

    def test_the_planners_hold_no_host_specific_field_names(self):
        # The other half of the same rule: a planner that reads `state_reason`
        # or `html_url` has learned one host's payload shape, and the next
        # host's is different. The forge hands back this repository's own
        # vocabulary instead.
        native = re.compile(r"\bstate_reason\b|\bhtml_url\b|\brepository_url\b"
                            r"|\bcheck_runs\b|\bmerge_status\b|\bweb_url\b"
                            r"|\bpull_request\b|\bsource_branch\b")
        for planner in ("heartbeat-issue-tick.py", "reviewer-tick.py",
                        "tester-tick.py"):
            with open(os.path.join(BUILDER, planner), encoding="utf-8") as f:
                hits = [l.strip() for l in f if native.search(l)]
            with self.subTest(planner=planner):
                self.assertEqual(hits, [])

    def test_the_allowlist_names_files_that_exist(self):
        # A stale entry is an exemption nobody is watching. Deleting the file
        # or renaming it has to take the exemption with it.
        for rel in sorted(ALLOWED):
            with self.subTest(file=rel):
                self.assertTrue(os.path.exists(os.path.join(BUILDER, rel)),
                                f"{rel} is exempted but does not exist")

    def test_the_check_would_actually_catch_something(self):
        # A regex that matches nothing passes every test in this file. Pin it
        # against the shapes it exists to find.
        for line in ('url = f"https://api.github.com/repos/{repo}/issues"',
                     'curl "$GH_API/repos/$REPO/pulls/$N/merge"',
                     'gl_get(f"/projects/{pid}/merge_requests/{iid}/notes")',
                     'r = get(f"{GITLAB_URL}/api/v4/issues")'):
            with self.subTest(line=line):
                self.assertTrue(HOSTS.search(line) or PATHS.search(line), line)
        for line in ("# every project is on the allowed list",
                     'path = os.path.join(home, "projects", repo)',
                     "issues_by_repo = FORGES.assigned_open_issues()"):
            with self.subTest(line=line):
                self.assertFalse(HOSTS.search(line) or PATHS.search(line), line)


if __name__ == "__main__":
    unittest.main()

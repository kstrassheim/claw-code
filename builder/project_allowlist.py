"""project_allowlist: which repositories is the bot PERMITTED to work on?

Imported by `project-allow` (the chat-facing CLI that edits the list) and by
every planner — heartbeat-issue-tick, tester-tick, the reviewer spawner — so
they cannot drift apart on what a repository reference is, or on what an
absent list means. Same reasoning as project-kind.sh for project detection:
one definition, four readers.

THE PROBLEM THIS SOLVES
-----------------------
Every subsystem discovers its own work from the GitHub API, and every one of
those queries is ACCOUNT-WIDE — none of them is scoped to a repository the
owner picked:

  issue-solver  GET /issues            every issue assigned to the bot,
                                       in any repository on the account
  reviewer      review-requested PRs   any repository that asks for its review
  tester        GET /user/repos        every repository it collaborates on

So anyone who can assign the bot an issue, request its review, or add it as a
collaborator could put it to work on their repository — checking the code out,
running an agent on it, pushing branches, commenting. Assignment, review
request and collaboration are not permission; they are how someone ASKS. This
list is the answer, and only the bot's owner writes it (`project-allow`,
surfaced in chat as `projects add|revoke|list`).

WHERE IT LIVES
--------------
~/.openclaw/projects-allowed.list, on the workspace PVC — so it survives pod
restarts and redeploys, which an env var in the Deployment would not (that
needs a secret edit and a rollout to change, and a revoke that needs a rollout
is a revoke that happens late).

One `owner/repo` per line, `#` comments and blank lines ignored:

    # granted 2026-08-14 by the owner
    octocat/hello-world
    acme-corp/web-test

Edits go through `project-allow`, which writes atomically and appends to
projects-allowed.log. Nothing stops a human editing the file by hand; the
parser is deliberately forgiving about whitespace, URLs and .git suffixes so a
hand edit is unlikely to silently mean nothing.

FAIL CLOSED
-----------
No file, empty file, unreadable file, pod unreachable → NO repository is
allowed and every subsystem idles. The alternative (fall back to "everything")
would turn any read failure into exactly the unrestricted behaviour this
exists to prevent. An idle tick costs one cycle and is visible in the plan
JSON as a `reason`; a permissive tick is the bot working on a repository
nobody permitted.
"""

import os
import re
import unicodedata

# Relative to the openclaw state dir, so both in-pod code (read_local) and the
# planners' `kubectl exec` snippet (pod_read_snippet) name the same file once.
LIST_REL = "projects-allowed.list"
LOG_REL = "projects-allowed.log"

# A GitHub repository is EXACTLY two segments: owner/repo. Nothing nests.
#
# Owner: 1-39 chars, alphanumerics and hyphens, never starting or ending with
# one. Repo: alphanumerics, hyphen, underscore and dot.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_OWNER_MAX = 39
_REPO_MAX = 100

# What may follow owner/repo in a URL a human copied out of the browser. This
# is an ALLOWLIST rather than "take the first two segments", because taking
# the first two would turn any three-segment typo into a confident grant of a
# repository the owner never named — and this file is the security boundary,
# so an unrecognised shape must be a question, not a guess.
_SUBRESOURCES = frozenset("""
    actions activity archive blame blob branches commit commits community
    compare contributors deployments discussions find forks graphs issues
    labels milestones network packages projects pulls pull pulse raw releases
    security settings stargazers tags tree watchers wiki
""".split())

# The bot authenticates against api.github.com and nothing else. A reference
# on another host cannot be worked on at all, and quietly reducing it to
# owner/repo would permit a DIFFERENT repository that happens to share the
# name. GITHUB_HOST exists for a GitHub Enterprise deployment.
_DEFAULT_HOSTS = ("github.com", "www.github.com", "api.github.com")


def state_root() -> str:
    return os.path.join(os.environ.get("HOME", "/home/node"), ".openclaw")


def list_path() -> str:
    return os.path.join(state_root(), LIST_REL)


def log_path() -> str:
    return os.path.join(state_root(), LOG_REL)


def allowed_hosts() -> set[str]:
    hosts = set(_DEFAULT_HOSTS)
    extra = os.environ.get("GITHUB_HOST", "").strip().lower().strip("/")
    if extra:
        hosts.update({extra, f"www.{extra}", f"api.{extra}"})
    return hosts


def normalize(raw: str) -> str:
    """A user-typed repository reference → bare `owner/repo`, or "" if it
    could not be one.

    Accepts what a human actually pastes into chat: the browser URL of the
    repository, of an issue, of a pull request; the HTTPS or SSH clone URL; a
    stray trailing slash. Getting this wrong is expensive in a way an ordinary
    parse error is not — a reference that does not match is not a loud
    failure, it is the bot quietly never working on that repository again — so
    it is worth being generous about the shape and strict about the result.
    """
    s = (raw or "").strip().strip("\"'").strip()
    if not s:
        return ""
    # Unicode dashes: a name pasted out of a chat client or a wiki table often
    # comes back with an en-dash where the user typed a hyphen. It would match
    # nothing, forever, and look identical in the reply.
    s = "".join("-" if unicodedata.category(c) == "Pd" else c for c in s)
    # ?tab=readme-ov-file, #readme — neither can occur inside owner/repo.
    s = s.split("?", 1)[0].split("#", 1)[0]

    host = ""
    if "://" in s:
        # scheme://[userinfo@]host[:port]/owner/repo/...
        s = s.split("://", 1)[1]
        host, _, s = s.partition("/")
    else:
        head = s.split("/", 1)[0]
        if "@" in head and ":" in head:
            # scp-style clone URL: git@github.com:owner/repo.git
            host, _, s = s.partition(":")

    if host:
        host = host.rsplit("@", 1)[-1].partition(":")[0].strip().lower()
        host = host.rstrip(".")
        if host not in allowed_hosts():
            return ""
        if host.startswith("api."):
            # The API URL for a repository is /repos/{owner}/{repo}.
            if not s.lower().startswith("repos/"):
                return ""
            s = s[len("repos/"):]

    s = s.strip("/")
    if not s:
        return ""
    segs = s.split("/")
    if len(segs) < 2:
        return ""
    if len(segs) > 2 and segs[2].casefold() not in _SUBRESOURCES:
        return ""

    owner, repo = segs[0], segs[1]
    if repo.endswith(".git") and len(repo) > len(".git"):
        repo = repo[: -len(".git")]
    if not _OWNER_RE.match(owner) or len(owner) > _OWNER_MAX:
        return ""
    if not _REPO_RE.match(repo) or len(repo) > _REPO_MAX:
        return ""
    if repo.strip(".") == "":
        # "." and ".." are path traversal dressed as a repository name.
        return ""
    return f"{owner}/{repo}"


def parse(text: str) -> list[str]:
    """File content → ordered, de-duplicated `owner/repo` entries.

    Unparseable lines are dropped: the file is a permission grant, and a line
    that cannot be understood has not granted anything.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        repo = normalize(line)
        if not repo:
            continue
        key = repo.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(repo)
    return out


class Allowlist:
    """The permitted set, plus whether we managed to READ it at all.

    `available` is the distinction that matters: an empty list means the owner
    has permitted nothing, an unavailable one means we could not find out.
    Both deny — see FAIL CLOSED above — but they are different operational
    situations and the planners report them with different reasons.
    """

    def __init__(self, entries: list[str], available: bool = True) -> None:
        self.entries = entries
        self.available = available
        self._keys = {e.casefold() for e in entries}

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, repo: str) -> bool:
        return self.allows(repo)

    def allows(self, repo: str) -> bool:
        if not self.available:
            return False
        norm = normalize(repo)
        # Compare case-insensitively: GitHub resolves owner and repository
        # names case-insensitively, so `Octocat/Hello-World` and
        # `octocat/hello-world` are one repository and must not be two
        # permission states.
        return bool(norm) and norm.casefold() in self._keys

    def deny_reason(self, repo: str) -> str:
        """Short machine-readable reason for a plan entry / log line. "" if
        allowed. The spawners match on these strings to tell "could not read
        the list" from "not on the list" — two situations that want different
        responses from an operator — so they are a contract, not prose."""
        if self.allows(repo):
            return ""
        if not self.available:
            return "allowlist-unavailable"
        if not self.entries:
            return "allowlist-empty"
        return "not-permitted"

    @staticmethod
    def denied() -> "Allowlist":
        return Allowlist([], available=False)


def read_local() -> Allowlist:
    """Read the list from this container's own filesystem. Used inside the
    openclaw pod (the CLI, and the runners' guard)."""
    try:
        with open(list_path(), encoding="utf-8") as f:
            return Allowlist(parse(f.read()))
    except FileNotFoundError:
        # Never permitted anything yet. Empty, but we did read it.
        return Allowlist([])
    except OSError:
        return Allowlist.denied()


# --- planner side: reading the list out of the openclaw pod ----------------
#
# The planners run in their own CronJob pods and already `kubectl exec` into
# the openclaw pod once per tick to inspect locks and state markers. The
# allowlist lives on that pod's PVC, so it rides along in the SAME exec as one
# more section rather than costing another round trip.
#
# `|| true` matters: those scripts run under `set -eu`, where a missing file
# would abort the whole state query and take the lock/marker sections with it.

ALLOWED_MARKER = "==ALLOWED=="


def pod_read_snippet() -> str:
    return (
        f'echo "{ALLOWED_MARKER}"; '
        f'cat "$HOME/.openclaw/{LIST_REL}" 2>/dev/null || true; '
    )


def from_section(lines: list[str], available: bool = True) -> Allowlist:
    """Build the allowlist from the lines the exec printed under ==ALLOWED==.

    `available=False` is for the case that matters: the exec itself failed. No
    lines then means "we could not ask", which must not be read as "the owner
    permitted nothing" — both deny, but only one of them is a fault to fix.
    """
    if not available:
        return Allowlist.denied()
    return Allowlist(parse("\n".join(lines)))

"""forge: the questions this bot asks a code host, and one answer per host.

WHY AN INTERFACE AND NOT A CLIENT
---------------------------------
The planners and the runners spend their time deciding things: is this issue
parked, is this head green, has a person answered, may this be merged. Every
one of those decisions used to be written in the same expression as the
request that answered it, so "should this be parked?" and "which URL says so?"
were one piece of code. Adding a second code host then means editing every
place a decision is made, rather than adding one implementation.

So the seam is drawn around the QUESTIONS, not around anybody's REST API.
`Forge` below is the list of things the bot actually wants to know. Each
implementation answers them however its platform can, and the callers never
learn which platform answered.

TWO SHAPES ARE LOAD-BEARING, AND BOTH WERE LEARNED FROM AN OUTAGE
-----------------------------------------------------------------
`checks_state(repo, sha)` returns a DECISION — one of `green`, `failed`,
`pending`, `none` — and never raw payloads. One host reports CI in two
unrelated places that have to be merged; another has a single pipeline state.
If callers saw the raw data they would each re-derive the reduction, slightly
differently, and the difference would be invisible until a red pull request
was reviewed as green. `none` is deliberately NOT `pending`: a repository
with no CI configured at all is not a build in progress, and a gate that
confuses the two waits forever on every such repository.

`close_issue(repo, n, delivered)` takes the INTENT — the work shipped, or it
was called off — and never a platform reason string. One host records that in
a native close reason; another has no such field and has to say it another
way. The caller knows which of the two happened; it must not know, or care,
how the platform writes it down.

THE NEUTRAL SHAPES
------------------
Everything handed back is this repository's own vocabulary, so a caller can
be read without knowing where the data came from:

  issue          {"forge", "repo", "number", "title", "body", "url",
                  "labels": [name, ...], "state": "open"|"closed",
                  "closedAs": None|"delivered"|"revoked"}

  note           {"id", "body", "author": {"username": ...}}
                 — the shape lexical_guard already speaks, so the guard has
                 one definition of "has the bot already asked this?" rather
                 than one per platform.

  change request {"forge", "repo", "number", "title", "body", "url",
                  "state": "open"|"closed"|"merged", "draft": bool,
                  "headSha", "headRef", "baseRef", "labels", "mergeable"}
                 — a pull request or a merge request; the callers only ever
                 needed "the change somebody proposed".

  verdict        {"author", "verdict": "approved"|"changes_requested"
                  |"commented"|"dismissed", "body", "sha"}

  finding        {"id", "rule", "severity", "state", "title", "url"}

SELECTION IS PER ITEM, NEVER A GLOBAL SWITCH
--------------------------------------------
`configured()` returns a `Forges` holding one implementation per host whose
credentials are present — nothing else is constructed, so a deployment with
only one set of credentials behaves exactly as it did before there were two.
Discovery stamps every record it hands back with the forge it came from, and
`Forges.of(record)` reads that stamp. A repository is never routed by a
global mode flag, because the whole point is that both hosts are worked in
the same tick.

Env:
  GITHUB_TOKEN / GITHUB_API (or GITHUB_API_URL) — GitHub credentials & base
  GITLAB_API_TOKEN / GITLAB_URL                 — GitLab credentials & base
  GITEA_API_TOKEN / GITEA_URL                   — Gitea credentials & base
  AZDO_API_TOKEN / AZDO_ORG (or AZDO_ORG_URL)   — Azure DevOps PAT & org
"""

from __future__ import annotations

import abc
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 15

# The four answers `checks_state` may give. `none` is separate from `pending`
# on purpose — see the module docstring.
GREEN, FAILED, PENDING, NONE = "green", "failed", "pending", "none"

# How a close is recorded, in intent rather than in any platform's spelling.
DELIVERED = "delivered"
REVOKED = "revoked"

GITHUB = "github"
GITLAB = "gitlab"
GITEA = "gitea"
AZDO = "azdo"


class ForgeError(RuntimeError):
    """A request to a code host failed.

    Carries the status code and the host's own words when there are any, so a
    planner can report "401" rather than "something went wrong" without
    knowing which host it was talking to.
    """

    def __init__(self, message: str, *, code: int | None = None,
                 reason: str = "", url: str = "", body: str = ""):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.url = url
        self.body = body


class RateLimited(ForgeError):
    """The host refused because the budget for this token is spent.

    Named separately because it arrives as an ordinary permission failure on
    at least one host: a 403 on a tick that made one request reads like a
    missing scope, and is not one.
    """


class _StripAuthAcrossHosts(urllib.request.HTTPRedirectHandler):
    """Drop the credential when a redirect leaves the host it was minted for.

    GitHub answers `/actions/jobs/<id>/logs` with a 302 to blob storage, and
    that storage rejects a request carrying a GitHub token — the reply is
    `403 AuthenticationFailed` from Azure, which reads like a permissions
    problem with the PAT and is not one. urllib replays every header across a
    redirect; curl drops this one, which is why the same fetch works by hand
    and returned nothing here.

    Same-host redirects keep the header, or every ordinary API redirect would
    turn into a 401.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        here = urllib.parse.urlsplit(req.full_url).netloc.lower()
        there = urllib.parse.urlsplit(newurl).netloc.lower()
        if here != there:
            # Match on the lowercased name rather than passing a literal:
            # urllib stores header keys `.capitalize()`d, so it holds
            # `Private-token`, and `remove_header("Private-Token")` pops a key
            # that is not there and reports nothing. `Authorization` survives
            # that round trip unchanged, which is exactly why a literal here
            # looks like it works while the GitLab half quietly does not.
            for name in list(new.headers):
                if name.lower() in ("authorization", "private-token"):
                    new.remove_header(name)
        return new


_OPENER = urllib.request.build_opener(_StripAuthAcrossHosts)


def _http(method: str, url: str, *, headers: dict, params: dict | None = None,
          json_body=None, form_body: dict | None = None,
          timeout: int = HTTP_TIMEOUT, raw: bool = False):
    """The one place in this module where a request leaves the process.

    Injected as `transport=` by the tests, which is what keeps every test in
    this repository network-free without also stubbing out the request
    building — the part most likely to be wrong.

    Returns the parsed JSON body, or None when the response had no body.
    With `raw=True` the body is returned as text instead — CI job logs are
    plain text, and JSON-parsing them threw the log away and answered None,
    which the caller could only read as "no log".
    Raises ForgeError for anything that is not a 2xx.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    headers = dict(headers)
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            body_bytes = r.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        except Exception:  # noqa: BLE001 - the error is what matters, not this
            body = ""
        head = e.headers or {}
        if e.code == 403 and head.get("X-RateLimit-Remaining") == "0":
            raise RateLimited(
                f"rate limit exhausted on {url} "
                f"(resets at {head.get('X-RateLimit-Reset', '?')})",
                code=e.code, reason=str(e.reason or ""), url=url, body=body,
            ) from None
        raise ForgeError(f"{e.code} on {url}: {body[:200]}",
                         code=e.code, reason=str(e.reason or ""),
                         url=url, body=body) from None
    if raw:
        return body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
    if not body_bytes:
        return None
    try:
        return json.loads(body_bytes)
    except ValueError:
        return None


class Forge(abc.ABC):
    """Everything the bot needs to know about a code host.

    Implementations answer in the neutral shapes described at the top of this
    module. A method a platform cannot answer yet raises NotImplementedError
    with a sentence saying so — never a silent empty result, which a caller
    would read as "there is nothing there".
    """

    #: `github` / `gitlab`. Stamped onto every record this forge hands back,
    #: and what `Forges.of` routes on.
    kind: str = ""

    #: What a human calls the thing this host proposes changes with. Used in
    #: anything a person reads, because "pull request" in a merge-request
    #: project is a bug report waiting to happen.
    change_request_noun: str = "change request"

    # -- identity -------------------------------------------------------

    @abc.abstractmethod
    def bot_identity(self) -> str:
        """The account name these credentials authenticate as.

        Resolved rather than configured: sibling deployments run under
        different accounts, and a hardcoded name makes "has the bot already
        said this?" answer about somebody else.
        """

    # -- discovery ------------------------------------------------------

    @abc.abstractmethod
    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Every open issue assigned to the bot, keyed by `owner/name`."""

    @abc.abstractmethod
    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Open change requests this bot should look at, newest first.

        Records are stubs — repo, number, labels — because the listing
        endpoints do not carry a head commit anywhere. `change_request` is
        what fills that in, and it costs a call per candidate.
        """

    @abc.abstractmethod
    def accessible_repos(self, limit: int) -> list[str]:
        """Repositories these credentials can reach, most recently pushed
        first. A fallback candidate source only — see tester-tick on why an
        activity-ordered list must not be the normal path."""

    @abc.abstractmethod
    def default_branch_head(self, repo: str) -> tuple[str, str]:
        """(default branch, head sha). Empty strings when it cannot be read;
        the caller treats that as "skip this repository"."""

    # -- issues ---------------------------------------------------------

    @abc.abstractmethod
    def issue(self, repo: str, number: int) -> dict:
        """One issue, in the neutral shape. {} when it cannot be read."""

    @abc.abstractmethod
    def comments(self, repo: str, number: int) -> list[dict]:
        """Every note on an issue, oldest first."""

    @abc.abstractmethod
    def post_comment(self, repo: str, number: int, body: str) -> bool:
        """Say something on an issue. False on any failure, never raises:
        a planner that cannot comment must still plan the rest of the tick."""

    @abc.abstractmethod
    def add_labels(self, repo: str, number: int, labels) -> bool:
        """Add labels to an issue. Mutual exclusion between `scope::value`
        labels is issue_status' business, not the transport's."""

    @abc.abstractmethod
    def remove_label(self, repo: str, number: int, label: str) -> bool:
        """Take one label off an issue."""

    @abc.abstractmethod
    def ensure_label(self, repo: str, name: str, color: str = "",
                     description: str = "") -> bool:
        """Make sure a label EXISTS in a repository, whatever that costs here.

        At least one host refuses to put a label on an issue until the label
        has been defined in the repository, so the first write of any new label
        in a fresh project fails with a status that reads like a permissions
        problem and is not one. The colour and the description come from the
        caller: they are the bot's vocabulary, not the host's.

        Already existing is SUCCESS. Every call after the first is a no-op, and
        a caller that read "it is already there" as a failure would give up on
        labelling anything for the second time.
        """

    @abc.abstractmethod
    def close_issue(self, repo: str, number: int, delivered: bool) -> bool:
        """Close an issue, recording WHETHER THE WORK SHIPPED.

        `delivered=True` means the change was made and merged;
        `delivered=False` means it was called off — a duplicate, a refusal, a
        decision not to do it. The caller knows which; each platform records
        it in whatever field or label it has for the purpose.
        """

    # -- change requests ------------------------------------------------

    @abc.abstractmethod
    def open_change_requests_for_issue(self, repo: str,
                                       number: int) -> list[int]:
        """Open change requests that would close this issue."""

    @abc.abstractmethod
    def change_request(self, repo: str, number: int) -> dict:
        """One change request, in the neutral shape. {} when unreadable."""

    @abc.abstractmethod
    def change_request_comments(self, repo: str, number: int) -> list[dict]:
        """Every note on a change request, oldest first.

        Separate from `comments` because the two are separate things on at
        least one host, and because a caller asking about a change request
        should say so.
        """

    @abc.abstractmethod
    def checks_state(self, repo: str, sha: str) -> str:
        """CI for one commit, reduced to green / failed / pending / none.

        THE ONLY PLACE CHECK SEMANTICS ARE DECIDED. Every caller gates on the
        answer and none of them may re-derive it. Anything unknown — an
        unreadable state, an unrecognised conclusion, a lookup that failed —
        is `pending`, because an unknown CI state must never read as green.
        """

    @abc.abstractmethod
    def checks(self, repo: str, sha: str) -> list[dict]:
        """Each check on a commit as {name, state}, state in the same four.

        `checks_state` answers "may this be merged"; this answers "what is
        running, and how did each one end" — which is what a summary for a
        person and a fingerprint for a change-detector need. Both come from
        the same reduction, so a check that reads failed in one cannot read
        pending in the other.
        """

    @abc.abstractmethod
    def review_verdicts(self, repo: str, number: int) -> list[dict]:
        """Reviews left on a change request, oldest first."""

    @abc.abstractmethod
    def merge(self, repo: str, number: int, squash: bool = True,
              delete_branch: bool = True) -> bool:
        """Merge a change request. False on any refusal."""

    @abc.abstractmethod
    def security_findings(self, repo: str, number: int) -> list[dict]:
        """Security findings the host itself raised against a change."""

    @abc.abstractmethod
    def post_change_request_comment(self, repo: str, number: int,
                                    body: str) -> bool:
        """Say something ON THE CHANGE REQUEST, not on the issue behind it.

        NOT the same call as `post_comment`, however much it looks like it on
        one host. GitHub models a pull request as an issue, so commenting on
        `/issues/<pr>/comments` happens to land on the pull request — and that
        coincidence hid the bug: on GitLab the same number addresses an
        ISSUE with that iid, which is a different item entirely and usually
        somebody else's work.

        Anything a person reads next to the diff belongs here: a review
        verdict, a note about what the bot is waiting for. Anything about the
        WORK ITEM belongs on the issue.
        """

    @abc.abstractmethod
    def close_change_request(self, repo: str, number: int) -> bool:
        """Close a change request WITHOUT merging it.

        The counterpart to `merge`, and until now the missing half: a change
        request could only ever end by landing. Asked to abandon one — a
        superseded approach, work called off, an issue closed as won't-do —
        the bot had no verb for it and quietly did something else instead.

        Abandoning is not deleting. The branch survives this call; removing it
        is `delete_branch`, deliberately separate so that closing a change
        request cannot destroy the only copy of the work as a side effect.
        """

    @abc.abstractmethod
    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete a branch. Refuses the default branch, on every host.

        Standalone, because until now a branch could only be removed as a
        side effect of merging — so a change request that was abandoned rather
        than landed left its branch behind forever.

        IRREVERSIBLE in a way the rest of this interface is not: a closed
        issue reopens and a closed change request reopens, but commits reachable
        only from a deleted branch are gone. Implementations must refuse the
        default branch rather than trusting the caller, and callers should
        treat False as "it is still there" rather than retrying.
        """

    @abc.abstractmethod
    def change_request_files(self, repo: str, number: int) -> list[dict]:
        """What a change touches: [{path, added, removed, status}].

        A summary, not a diff. The reviewer has its own checkout and reads the
        diff from git; this is the shape of the change, which is what decides
        whether it is small enough to read in one sitting.
        """

    @abc.abstractmethod
    def submit_review(self, repo: str, number: int, verdict: str,
                      body: str) -> bool:
        """Leave a REVIEW — `approve`, `request-changes` or `comment`.

        Distinct from a comment, and worth the separate method because the
        hosts disagree about it fundamentally: one refuses a review from the
        change's own author, and the bot authors everything it opens. So this
        failing is NORMAL and is not the verdict — the verdict is the comment,
        which is what the merge gate reads. Returning False here means the
        review was not recorded, never that the work was rejected.
        """

    @abc.abstractmethod
    def review_requests(self, repo: str, number: int) -> list[str]:
        """Who has been asked to review, by account name."""

    @abc.abstractmethod
    def request_review(self, repo: str, number: int, reviewers) -> bool:
        """Ask accounts to review a change.

        False covers the refusal that matters: at least one host will not let
        an author request review of their own work, and the bot is usually
        both. A caller must treat that as "nobody was asked", not as an error
        worth stopping for.
        """

    @abc.abstractmethod
    def remove_review_request(self, repo: str, number: int,
                              reviewers) -> bool:
        """Withdraw a review request. Used to enforce "not until CI is green"."""

    @abc.abstractmethod
    def react(self, repo: str, number: int, comment_id, emoji: str) -> bool:
        """Acknowledge one comment, so a person can see it was read.

        Takes the ITEM as well as the comment: one host addresses a comment
        globally and the other only within the item it belongs to. Asking for
        both is what lets either answer.
        """

    @abc.abstractmethod
    def failing_check_log(self, repo: str, sha: str,
                          limit: int = 20000) -> str:
        """The output of the first failing check on a commit, or "".

        This is how the solver learns WHY CI is red rather than only that it
        is. Truncated from the END, because a failure says what went wrong on
        its last lines and the first lines are setup.
        """

    @abc.abstractmethod
    def recent_change_requests(self, repo: str, state: str = "closed",
                               limit: int = 50) -> list[dict]:
        """Change requests by most recently updated, in the neutral shape."""

    @abc.abstractmethod
    def open_issues(self, repo: str, limit: int = 100) -> list[dict]:
        """Every open issue in one repository, in the neutral shape."""

    @abc.abstractmethod
    def create_issue(self, repo: str, title: str, body: str = "",
                     labels=None, assignees=None) -> int:
        """Open an issue. Returns its number, or 0 if it could not be opened.

        `assignees` is not decoration: the solver's whole queue is "issues
        assigned to me", so an issue this bot files for itself and does not
        assign is an issue nobody ever works.
        """

    @abc.abstractmethod
    def branch_head(self, repo: str, branch: str) -> str:
        """The commit at the tip of a branch, or "" when it cannot be read."""

    @abc.abstractmethod
    def file_at_ref(self, repo: str, path: str, ref: str) -> str:
        """One file's TEXT at one commit, or "" when it is not there.

        Empty means "not present", which is a legitimate answer — the callers
        read optional configuration files that most repositories do not have.
        """

    @abc.abstractmethod
    def comment_on_commit(self, repo: str, sha: str, body: str) -> bool:
        """Say something against a COMMIT rather than against an item."""




class Forges:
    """The configured hosts, and which one a given item came from.

    Selection is per item. Every record discovery hands back carries the
    `forge` it came from, and `of()` reads that stamp — there is no mode flag
    anywhere, because both hosts are worked in the same tick and a switch
    would mean choosing one of them per deployment.

    Repositories that were NOT discovered (the tester's candidates come from
    the owner's permitted list, which is a list of names and nothing more) are
    routed by what discovery recorded for them earlier in the tick, and
    failing that by the first configured host. With one host configured that
    is exactly today's behaviour; with two it is the reason `remember()`
    exists.
    """

    def __init__(self, forges=()):
        self._forges = list(forges)
        self._by_kind = {f.kind: f for f in self._forges}
        self._by_repo: dict[str, Forge] = {}

    def __iter__(self):
        return iter(self._forges)

    def __len__(self):
        return len(self._forges)

    def __bool__(self):
        return bool(self._forges)

    def kinds(self) -> list[str]:
        return [f.kind for f in self._forges]

    def by_kind(self, kind: str):
        return self._by_kind.get(kind)

    def remember(self, repo: str, f: Forge) -> None:
        """Record where a repository was discovered."""
        if repo:
            self._by_repo[repo] = f

    def of(self, item):
        """The forge an item belongs to.

        `item` is either a record carrying a `forge` stamp, or a repository
        name. Raises when nothing is configured at all, because a caller
        acting on "no forge" would silently do nothing.
        """
        if not self._forges:
            raise ForgeError("no forge is configured")
        if isinstance(item, dict):
            stamped = self._by_kind.get(item.get("forge") or "")
            if stamped is not None:
                return stamped
            item = item.get("repo") or ""
        found = self._by_repo.get(str(item or ""))
        return found if found is not None else self._forges[0]

    # -- cross-host discovery -------------------------------------------

    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Every host's assigned open issues, merged and remembered.

        A repository name is unique within a host but not across them, so the
        mapping recorded here is what lets every later question about that
        repository go back to the host that answered the first one.
        """
        by_repo: dict[str, list[dict]] = {}
        for f in self._forges:
            for repo, issues in f.assigned_open_issues().items():
                self.remember(repo, f)
                by_repo.setdefault(repo, []).extend(issues)
        return by_repo

    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Every host's review candidates, merged and remembered.

        The cap is applied per host and then again to the merged list, so one
        busy host cannot crowd the other out of a tick entirely.
        """
        out: list[dict] = []
        for f in self._forges:
            for item in f.reviewable_change_requests(limit):
                self.remember(item.get("repo") or "", f)
                out.append(item)
        return out[:limit]

    def accessible_repos(self, limit: int) -> list[str]:
        out: list[str] = []
        for f in self._forges:
            for repo in f.accessible_repos(limit):
                self.remember(repo, f)
                out.append(repo)
        return out[:limit]



# ---------------------------------------------------------------------------
# the hosts
# ---------------------------------------------------------------------------
#
# Imported at the BOTTOM because each host module imports the ABC and the
# transport from this one. Flat siblings rather than a package: a ConfigMap key
# cannot contain a slash, and that ConfigMap is how builder code reaches a pod
# without rebuilding the image.
#
# Re-exported so `forge.GitHubForge` keeps working for every caller and every
# test — the seam is the module name, not the file layout.

from forge_azdo import AzureDevOpsForge  # noqa: E402,F401
from forge_gitea import GiteaForge  # noqa: E402,F401
from forge_github import GitHubForge, _repo_from_api_url  # noqa: E402,F401
from forge_gitlab import GitLabForge, _project_from_web_url  # noqa: E402,F401

def configured(env=None) -> Forges:
    """The hosts this deployment has credentials for.

    Each is skipped when its credentials are unset, and skipped SILENTLY:
    "GitLab is not configured here" is the normal state of most deployments,
    not a fault to report. A deployment with only one set of credentials
    therefore constructs exactly one implementation and behaves precisely as
    it did before there were two.
    """
    env = os.environ if env is None else env
    out = []
    token = env.get("GITHUB_TOKEN", "")
    if token:
        out.append(GitHubForge(
            token,
            env.get("GITHUB_API") or env.get("GITHUB_API_URL")
            or "https://api.github.com"))
    gitlab_url = (env.get("GITLAB_URL", "") or "").rstrip("/")
    gitlab_token = env.get("GITLAB_API_TOKEN", "")
    if gitlab_url and gitlab_token:
        out.append(GitLabForge(gitlab_url, gitlab_token))
    gitea_url = (env.get("GITEA_URL", "") or "").rstrip("/")
    gitea_token = env.get("GITEA_API_TOKEN", "")
    if gitea_url and gitea_token:
        out.append(GiteaForge(gitea_url, gitea_token))
    # Either spelling of the organisation. AZDO_ORG is the bare name, which
    # is what the CLI and the MCP server take as an argument; AZDO_ORG_URL is
    # the full base, kept for anything that needs to point elsewhere. Giving
    # the name is enough for all three, so a deployment sets ONE value.
    azdo_url = (env.get("AZDO_ORG_URL", "") or "").rstrip("/")
    if not azdo_url:
        org = (env.get("AZDO_ORG", "") or "").strip().strip("/")
        azdo_url = f"https://dev.azure.com/{org}" if org else ""
    azdo_token = env.get("AZDO_API_TOKEN", "")
    if azdo_url and azdo_token:
        out.append(AzureDevOpsForge(azdo_url, azdo_token))
    return Forges(out)

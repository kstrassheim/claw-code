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


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


# What a completed check-run conclusion means for the gate.
#
# `action_required` is a failure on purpose: it will never turn green on its
# own, so waiting for it is waiting forever. `stale` is NOT — a run is marked
# stale when a newer one supersedes it, so the honest answer is "the real
# answer has not arrived yet".
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
FAILING_CONCLUSIONS = frozenset({
    "failure", "timed_out", "cancelled", "canceled", "action_required",
    "startup_failure",
})

# The native close reasons, and what each one means to this bot.
_COMPLETED = "completed"
_NOT_PLANNED = "not_planned"


class GitHubForge(Forge):
    """GitHub, over its REST API.

    Every quirk here was written from a failure in production, and each is
    commented where it lives rather than in a list nobody reads.
    """

    kind = GITHUB
    change_request_noun = "pull request"

    def __init__(self, token: str, api: str = "https://api.github.com",
                 *, transport=_http, user_agent: str = "openclaw-forge/1.0"):
        self.token = token or ""
        self.api = (api or "https://api.github.com").rstrip("/")
        self._transport = transport
        self._user_agent = user_agent
        self._identity: str | None = None

    # -- transport ------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self._user_agent,
        }

    def _get(self, path: str, params: dict | None = None):
        return self._transport("GET", f"{self.api}{path}",
                               headers=self._headers(), params=params)

    def _write(self, method: str, path: str, payload=None):
        """A write that reports rather than raises.

        Callers decide what a failed write means — usually "leave the item
        alone" — and none of them want a planner-wide crash for it.
        """
        try:
            self._transport(method, f"{self.api}{path}",
                            headers=self._headers(), json_body=payload)
            return True
        except Exception:  # noqa: BLE001 - any failure is "did not write"
            return False

    # -- identity -------------------------------------------------------

    def bot_identity(self) -> str:
        if self._identity is None:
            me = self._get("/user")
            self._identity = str((me or {}).get("login") or "") \
                if isinstance(me, dict) else ""
        return self._identity

    # -- neutral shapes -------------------------------------------------

    def _labels(self, raw) -> list[str]:
        """Label names, whatever shape the API handed back.

        Both shapes really do occur: the issues endpoints return objects, and
        some search payloads have been seen returning bare strings.
        """
        out = []
        for item in raw or []:
            name = item.get("name", "") if isinstance(item, dict) else item
            name = str(name or "").strip()
            if name:
                out.append(name)
        return out

    def _issue(self, raw: dict, repo: str = "") -> dict:
        state = str(raw.get("state") or "open").lower()
        closed_as = None
        if state == "closed":
            # An issue closed with no reason at all predates the field, and
            # every one of those was a delivery. `not_planned` is the only
            # value that means the work was called off.
            reason = str(raw.get("state_reason") or "").lower()
            closed_as = REVOKED if reason == _NOT_PLANNED else DELIVERED
        return {
            "forge": self.kind,
            "repo": repo or _repo_from_api_url(raw.get("repository_url") or ""),
            "number": raw.get("number"),
            "title": raw.get("title") or "",
            "body": raw.get("body") or "",
            "url": raw.get("html_url") or "",
            "labels": self._labels(raw.get("labels")),
            "state": state,
            "closedAs": closed_as,
            # WHO ASKED. The account to hand the finished work back to — and
            # it is not always the repo owner: the bot files issues itself
            # (the tester does), and asking the bot to review the bot is not
            # a sign-off. Callers fall back when this is the bot's own login.
            "author": ((raw.get("user") or {}).get("login") or ""),
            # This host serves change requests from the ISSUES collection as
            # well, and they arrive looking exactly like issues apart from one
            # extra key. A caller that sizes one, or plans work on one, is
            # estimating its own output — so the distinction is answered here
            # rather than left to whoever remembers the key's name.
            "isChangeRequest": "pull_request" in raw,
            # WHEN it was filed. The delivery sweep needs it to tell a change
            # that closed this issue from one that landed before it existed.
            "createdAt": raw.get("created_at") or "",
        }

    def _note(self, raw: dict) -> dict:
        return {
            "id": raw.get("id"),
            "body": raw.get("body") or "",
            "author": {"username": ((raw.get("user") or {}).get("login") or "")},
            # WHEN it was said. "Has a person replied since the bot asked?" and
            # "is this verdict from THIS run?" are both questions about time,
            # and a reader without one falls back to "the newest comment",
            # which reads an old answer as a new one.
            "createdAt": raw.get("created_at") or "",
        }

    def _change_request(self, raw: dict, repo: str) -> dict:
        head = raw.get("head") or {}
        base = raw.get("base") or {}
        state = str(raw.get("state") or "").lower()
        if raw.get("merged_at") or raw.get("merged"):
            state = "merged"
        return {
            "forge": self.kind,
            "repo": repo,
            "number": raw.get("number"),
            "title": raw.get("title") or "",
            "body": raw.get("body") or "",
            "url": raw.get("html_url") or "",
            "state": state,
            "draft": bool(raw.get("draft")),
            "headSha": head.get("sha") or "",
            "headRef": head.get("ref") or "",
            "baseRef": base.get("ref") or "",
            "labels": self._labels(raw.get("labels")),
            "mergeable": raw.get("mergeable"),
            # Who opened it. The reviewer needs this to know whether it is
            # reviewing its own work, which decides whether a refused review
            # is a problem or the expected answer.
            "author": ((raw.get("user") or {}).get("login") or ""),
        }

    # -- discovery ------------------------------------------------------

    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Every open issue assigned to the bot, in ONE cross-repository call
        per page.

        Pull requests come back from the same endpoint and are dropped: they
        are the reviewer's business, and planning one as an issue would spawn
        a solver on the bot's own branch.
        """
        by_repo: dict[str, list[dict]] = {}
        page = 1
        while True:
            batch = self._get("/issues", {"filter": "assigned",
                                          "state": "open",
                                          "per_page": 100, "page": page})
            batch = batch if isinstance(batch, list) else []
            for raw in batch:
                if "pull_request" in raw:
                    continue
                repo = _repo_from_api_url(raw.get("repository_url") or "")
                if not repo:
                    continue
                by_repo.setdefault(repo, []).append(self._issue(raw, repo))
            if len(batch) < 100:
                break
            page += 1
        return by_repo

    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Open pull requests this bot should review: its OWN, plus any it was
        asked to review.

        TWO SEARCHES, AND THE FIRST ONE IS THE IMPORTANT ONE.

        The obvious design is "review what I was asked to review" — the solver
        requests the bot as reviewer, the reviewer picks it up. That is
        impossible here. GitHub refuses to let a pull request's AUTHOR be
        added as its reviewer:

            422  Review cannot be requested from pull request author.

        The bot authors every pull request it opens, so it can never appear in
        its own `review-requested:` results. Keying discovery on that alone
        deadlocks the whole pipeline silently: the solver waits for a verdict,
        the reviewer never sees the pull request, and every tick costs nothing
        and does nothing — which reads as "the bot is idle" rather than as a
        fault.

        So authorship is the primary signal. `review-requested:` is kept
        because it is the only way to catch a pull request a HUMAN asked the
        bot to look at, including via a team, which authorship would miss.

        Results are merged and de-duplicated: a pull request the bot authored
        AND was somehow requested on must be reviewed once, not twice.

        Search is a different service from the REST API and behaves like one —
        its own rate limit, its own response shape, and eventual consistency —
        which is why exactly these two requests are made and everything else
        is a core-API call.
        """
        login = self.bot_identity()
        if not login:
            return []

        def search(query: str) -> list[dict]:
            data = self._get("/search/issues", {
                "q": query, "sort": "updated", "order": "desc",
                "per_page": str(limit),
            })
            items = data.get("items", []) if isinstance(data, dict) else []
            return [i for i in items if isinstance(i, dict)]

        seen: set[tuple[str, int]] = set()
        out: list[dict] = []
        for query in (f"is:pr is:open author:{login}",
                      f"is:pr is:open review-requested:{login}"):
            for item in search(query):
                repo = _repo_from_api_url(item.get("repository_url") or "")
                key = (repo, item.get("number"))
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "forge": self.kind,
                    "repo": repo,
                    "number": item.get("number"),
                    "title": item.get("title") or "",
                    "labels": self._labels(item.get("labels")),
                })
        return out[:limit]

    def accessible_repos(self, limit: int) -> list[str]:
        # `affiliation=owner,collaborator` excludes org-membership noise, and
        # `type` is mutually exclusive with it (the API answers 422), so only
        # affiliation is sent.
        repos = self._get("/user/repos", {
            "affiliation": "owner,collaborator",
            "sort": "pushed", "direction": "desc",
            "per_page": str(limit),
        })
        rows = repos if isinstance(repos, list) else []
        return [r["full_name"] for r in rows][:limit]

    def default_branch_head(self, repo: str) -> tuple[str, str]:
        try:
            raw = self._get(f"/repos/{repo}")
        except Exception:  # noqa: BLE001
            return ("", "")
        branch = (raw or {}).get("default_branch") or "main"
        try:
            data = self._get(f"/repos/{repo}/branches/{branch}")
        except Exception:  # noqa: BLE001
            return (branch, "")
        sha = ((data.get("commit") or {}).get("sha")) or ""
        return (branch, sha)

    # -- issues ---------------------------------------------------------

    def issue(self, repo: str, number: int) -> dict:
        try:
            raw = self._get(f"/repos/{repo}/issues/{number}")
        except Exception:  # noqa: BLE001
            return {}
        return self._issue(raw, repo) if isinstance(raw, dict) else {}

    def comments(self, repo: str, number: int) -> list[dict]:
        rows = self._get(f"/repos/{repo}/issues/{number}/comments",
                         {"per_page": "100"})
        if not isinstance(rows, list):
            raise ForgeError(f"unexpected comments payload for {repo}#{number}")
        return [self._note(r) for r in rows if isinstance(r, dict)]

    def post_comment(self, repo: str, number: int, body: str) -> bool:
        return self._write("POST", f"/repos/{repo}/issues/{number}/comments",
                           {"body": body})

    def add_labels(self, repo: str, number: int, labels) -> bool:
        names = [str(l) for l in (labels or []) if str(l).strip()]
        if not names:
            return True
        return self._write("POST", f"/repos/{repo}/issues/{number}/labels",
                           {"labels": names})

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        enc = urllib.parse.quote(str(label), safe="")
        return self._write("DELETE",
                           f"/repos/{repo}/issues/{number}/labels/{enc}")

    def ensure_label(self, repo: str, name: str, color: str = "",
                     description: str = "") -> bool:
        if not name:
            return False
        payload = {"name": name, "color": color or "c5def5",
                   "description": description or ""}
        try:
            self._transport("POST", f"{self.api}/repos/{repo}/labels",
                            headers=self._headers(), json_body=payload)
            return True
        except ForgeError as exc:
            # 422 is what "a label by that name already exists" answers with,
            # and after the first estimate in a repository that is every call.
            # Anything else is a real failure and is reported as one.
            return exc.code == 422
        except Exception:  # noqa: BLE001
            return False

    def close_issue(self, repo: str, number: int, delivered: bool) -> bool:
        """Close, recording delivered versus called-off natively.

        `state_reason` is what separates the two here, and it is the reason
        this method takes an intent rather than a string: it is the field the
        status model reads back to tell `Done` from `Won't do`, and a caller
        that had to name the field would have to know it exists.
        """
        return self._write("PATCH", f"/repos/{repo}/issues/{number}", {
            "state": "closed",
            "state_reason": _COMPLETED if delivered else _NOT_PLANNED,
        })

    # -- change requests ------------------------------------------------

    def open_change_requests_for_issue(self, repo: str,
                                       number: int) -> list[int]:
        """Open pull requests that close this issue, by branch or by keyword.

        Both signals are needed: the runner names its branch after the issue,
        while a human writes `closes #n` in the description. A bare mention of
        the number is NOT a link — "unlike #5, this one" would otherwise
        attach somebody else's work to the issue.
        """
        rows = self._get(f"/repos/{repo}/pulls",
                         {"state": "open", "per_page": "100"})
        if not isinstance(rows, list):
            return []
        out = []
        for pr in rows:
            if not isinstance(pr, dict):
                continue
            head = ((pr.get("head") or {}).get("ref") or "")
            body = pr.get("body") or ""
            if head.startswith(f"issue-{number}-") or re.search(
                    rf"\b(clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed))\s+"
                    rf"#{number}\b", body, re.IGNORECASE):
                out.append(pr.get("number"))
        return [n for n in out if n]

    def change_request(self, repo: str, number: int) -> dict:
        """The pull request itself.

        Needed because neither the search results nor the pulls LIST endpoint
        carries the head sha the review gate turns on.
        """
        try:
            raw = self._get(f"/repos/{repo}/pulls/{number}")
        except Exception:  # noqa: BLE001
            return {}
        return self._change_request(raw, repo) if isinstance(raw, dict) else {}

    def change_request_comments(self, repo: str, number: int) -> list[dict]:
        # A pull request is an issue here, and its conversation lives on the
        # issue endpoint. Review comments are a different thing and are read
        # through review_verdicts.
        return self.comments(repo, number)

    def checks_state(self, repo: str, sha: str) -> str:
        """Two calls, because the two CI mechanisms are two endpoints.

        Actions and anything using the Checks API report as check-runs; older
        integrations post classic commit statuses. A repository can use
        either, or both, and a gate that reads only one of them approves a
        pull request whose real CI is red.

        A lookup that fails is `pending`: an unknown CI state must never read
        as green, and there is no sha to check before there is a commit.
        """
        if not sha:
            return PENDING
        try:
            runs = self._get(f"/repos/{repo}/commits/{sha}/check-runs",
                             {"per_page": "100"})
        except Exception:  # noqa: BLE001
            return PENDING
        try:
            combined = self._get(f"/repos/{repo}/commits/{sha}/status",
                                 {"per_page": "100"})
        except Exception:  # noqa: BLE001
            return PENDING
        return self.reduce_checks(runs, combined)

    @staticmethod
    def reduce_checks(check_runs, combined_status) -> str:
        """Reduce a head commit's CI to one of green / failed / pending / none.

        Precedence is failed > pending > green:
          - a failure is decisive — waiting for the rest of a red build
            changes nothing;
          - anything unfinished, or any conclusion this code does not
            recognise, means wait. Never treat "I don't know what that means"
            as passing.

        `none` is its own answer: no check-runs and no statuses at all. That is
        a repository with no CI wired up, not a build in progress — and it
        must not be confused with `pending`, because an empty combined status
        reports `state: pending`, which a planner that believed it would wait
        on forever.
        """
        runs = (check_runs or {}).get("check_runs") or []
        statuses = (combined_status or {}).get("statuses") or []

        failed = pending = passed = 0

        for run in runs:
            if not isinstance(run, dict):
                continue
            status = str(run.get("status") or "").lower()
            conclusion = str(run.get("conclusion") or "").lower()
            if status != "completed" or not conclusion:
                pending += 1
            elif conclusion in FAILING_CONCLUSIONS:
                failed += 1
            elif conclusion in PASSING_CONCLUSIONS:
                passed += 1
            else:
                pending += 1

        for st in statuses:
            if not isinstance(st, dict):
                continue
            state = str(st.get("state") or "").lower()
            if state == "success":
                passed += 1
            elif state in ("failure", "error"):
                failed += 1
            else:
                # `pending`, and anything unrecognised.
                pending += 1

        if failed:
            return FAILED
        if pending:
            return PENDING
        if passed:
            return GREEN
        return NONE

    def checks(self, repo: str, sha: str) -> list[dict]:
        if not sha:
            return []
        out = []
        try:
            raw = self._get(f"/repos/{repo}/commits/{sha}/check-runs",
                            {"per_page": "100"})
        except Exception:  # noqa: BLE001
            raw = None
        for run in ((raw or {}).get("check_runs") or []
                    if isinstance(raw, dict) else []):
            if not isinstance(run, dict):
                continue
            status = str(run.get("status") or "").lower()
            conclusion = str(run.get("conclusion") or "").lower()
            if status != "completed" or not conclusion:
                state = PENDING
            elif conclusion in FAILING_CONCLUSIONS:
                state = FAILED
            elif conclusion in PASSING_CONCLUSIONS:
                state = GREEN
            else:
                state = PENDING
            out.append({"name": run.get("name") or "", "state": state})
        try:
            combined = self._get(f"/repos/{repo}/commits/{sha}/status")
        except Exception:  # noqa: BLE001
            combined = None
        for st in ((combined or {}).get("statuses") or []
                   if isinstance(combined, dict) else []):
            if not isinstance(st, dict):
                continue
            state = str(st.get("state") or "").lower()
            out.append({
                "name": st.get("context") or "",
                "state": GREEN if state == "success"
                else FAILED if state in ("failure", "error") else PENDING,
            })
        return out

    def review_verdicts(self, repo: str, number: int) -> list[dict]:
        rows = self._get(f"/repos/{repo}/pulls/{number}/reviews",
                         {"per_page": "100"})
        if not isinstance(rows, list):
            return []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append({
                "author": ((r.get("user") or {}).get("login") or ""),
                "verdict": _VERDICTS.get(
                    str(r.get("state") or "").upper(), "commented"),
                "body": r.get("body") or "",
                "sha": r.get("commit_id") or "",
            })
        return out

    def merge(self, repo: str, number: int, squash: bool = True,
              delete_branch: bool = True) -> bool:
        if not self._write("PUT", f"/repos/{repo}/pulls/{number}/merge",
                           {"merge_method": "squash" if squash else "merge"}):
            return False
        if delete_branch:
            # Best effort: the merge is the thing that mattered, and a branch
            # that outlives it is untidy rather than wrong.
            pr = self.change_request(repo, number)
            ref = pr.get("headRef") or ""
            if ref:
                self._write("DELETE", f"/repos/{repo}/git/refs/heads/{ref}")
        return True

    def post_change_request_comment(self, repo: str, number: int,
                                    body: str) -> bool:
        """A pull request IS an issue here, and shares its comment endpoint.

        Written out rather than delegated to `post_comment` so the two stay
        independently readable: they are the same call only on this host.
        """
        return self._write("POST", f"/repos/{repo}/issues/{number}/comments",
                           {"body": body})

    def close_change_request(self, repo: str, number: int) -> bool:
        """A pull request has no close REASON — only a state."""
        return self._write("PATCH", f"/repos/{repo}/pulls/{number}",
                           {"state": "closed"})

    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete a ref, refusing the default branch.

        The guard is here rather than in the caller because this is the layer
        that knows which branch is default, and because a caller that got it
        wrong would not get a second chance.
        """
        branch = str(branch or "").strip().lstrip("/")
        if not branch:
            return False
        try:
            if branch == (self.default_branch_head(repo) or ("", ""))[0]:
                return False
        except Exception:  # noqa: BLE001
            # Cannot tell what the default is, so cannot promise this is not
            # it. Refusing costs a branch that stays; guessing costs main.
            return False
        return self._write("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")

    def security_findings(self, repo: str, number: int) -> list[dict]:
        try:
            rows = self._get(f"/repos/{repo}/code-scanning/alerts",
                             {"ref": f"refs/pull/{number}/head",
                              "state": "open", "per_page": "100"})
        except Exception:  # noqa: BLE001
            # A repository without code scanning answers 404, and "the feature
            # is off" is not a finding.
            return []
        if not isinstance(rows, list):
            return []
        out = []
        for a in rows:
            if not isinstance(a, dict):
                continue
            rule = a.get("rule") or {}
            out.append({
                "id": a.get("number"),
                "rule": rule.get("id") or "",
                "severity": (rule.get("security_severity_level")
                             or rule.get("severity") or ""),
                "state": a.get("state") or "",
                "title": rule.get("description") or "",
                "url": a.get("html_url") or "",
            })
        return out


    # -- the rest of what the runners ask -------------------------------

    def change_request_files(self, repo: str, number: int) -> list[dict]:
        rows = self._get(f"/repos/{repo}/pulls/{number}/files",
                         {"per_page": "100"})
        if not isinstance(rows, list):
            return []
        out = []
        for f in rows:
            if not isinstance(f, dict):
                continue
            out.append({
                "path": f.get("filename") or "",
                "added": int(f.get("additions") or 0),
                "removed": int(f.get("deletions") or 0),
                "status": f.get("status") or "",
            })
        return out

    def submit_review(self, repo: str, number: int, verdict: str,
                      body: str) -> bool:
        event = {"approve": "APPROVE",
                 "request-changes": "REQUEST_CHANGES",
                 "comment": "COMMENT"}.get(str(verdict or "").lower())
        if event is None:
            return False
        # Expected to fail when the bot reviews its own work, which is the
        # normal case here — the solver and the reviewer are one account.
        return self._write("POST", f"/repos/{repo}/pulls/{number}/reviews",
                           {"event": event, "body": body or ""})

    def review_requests(self, repo: str, number: int) -> list[str]:
        raw = self._get(f"/repos/{repo}/pulls/{number}/requested_reviewers")
        if not isinstance(raw, dict):
            return []
        return [str((u or {}).get("login") or "")
                for u in (raw.get("users") or []) if isinstance(u, dict)]

    def request_review(self, repo: str, number: int, reviewers) -> bool:
        names = [str(r) for r in (reviewers or []) if str(r).strip()]
        if not names:
            return False
        # 422 when the author is among them, which is this bot on its own
        # pull requests. `False` is the honest answer: nobody was asked.
        return self._write("POST",
                           f"/repos/{repo}/pulls/{number}/requested_reviewers",
                           {"reviewers": names})

    def remove_review_request(self, repo: str, number: int,
                              reviewers) -> bool:
        names = [str(r) for r in (reviewers or []) if str(r).strip()]
        if not names:
            return False
        return self._write("DELETE",
                           f"/repos/{repo}/pulls/{number}/requested_reviewers",
                           {"reviewers": names})

    def react(self, repo: str, number: int, comment_id, emoji: str) -> bool:
        # The item is not part of the address here; a comment id is unique
        # across the repository. It is taken anyway, because on the other host
        # it is the only way to find the comment at all.
        if not comment_id:
            return False
        return self._write(
            "POST", f"/repos/{repo}/issues/comments/{comment_id}/reactions",
            {"content": emoji or "eyes"})

    def failing_check_log(self, repo: str, sha: str,
                          limit: int = 20000) -> str:
        if not sha:
            return ""
        try:
            raw = self._get(f"/repos/{repo}/commits/{sha}/check-runs",
                            {"per_page": "100"})
        except Exception:  # noqa: BLE001
            return ""
        runs = (raw or {}).get("check_runs") or [] if isinstance(raw, dict) else []
        job_id = None
        for run in runs:
            if not isinstance(run, dict):
                continue
            if str(run.get("conclusion") or "").lower() in FAILING_CONCLUSIONS:
                # The check-run id IS the job id for runs this host produced
                # itself; a check posted by anything else has no log to fetch
                # and is skipped rather than guessed at.
                if str(run.get("app", {}).get("slug") or "") in (
                        "github-actions", ""):
                    job_id = run.get("id")
                    break
        if not job_id:
            return ""
        try:
            # Not JSON: this endpoint answers with the log itself, so the
            # shared reader would return None on it.
            text = self._transport(
                "GET", f"{self.api}/repos/{repo}/actions/jobs/{job_id}/logs",
                headers=self._headers(), raw=True)
        except Exception:  # noqa: BLE001
            return ""
        text = text if isinstance(text, str) else ""
        return text[-limit:] if limit and len(text) > limit else text

    def recent_change_requests(self, repo: str, state: str = "closed",
                               limit: int = 50) -> list[dict]:
        rows = self._get(f"/repos/{repo}/pulls",
                         {"state": state, "sort": "updated",
                          "direction": "desc", "per_page": str(limit)})
        if not isinstance(rows, list):
            return []
        return [self._change_request(r, repo) for r in rows
                if isinstance(r, dict)]

    def open_issues(self, repo: str, limit: int = 100) -> list[dict]:
        rows = self._get(f"/repos/{repo}/issues",
                         {"state": "open", "per_page": str(limit)})
        if not isinstance(rows, list):
            return []
        # The collection serves change requests too; an issue listing that
        # includes them makes the caller count its own output as work.
        return [self._issue(r, repo) for r in rows
                if isinstance(r, dict) and "pull_request" not in r]

    def create_issue(self, repo: str, title: str, body: str = "",
                     labels=None, assignees=None) -> int:
        payload = {"title": title, "body": body or ""}
        names = [str(l) for l in (labels or []) if str(l).strip()]
        if names:
            payload["labels"] = names
        who = [str(a) for a in (assignees or []) if str(a).strip()]
        if who:
            payload["assignees"] = who
        try:
            made = self._transport("POST", f"{self.api}/repos/{repo}/issues",
                                   headers=self._headers(), json_body=payload)
        except Exception:  # noqa: BLE001
            return 0
        return int((made or {}).get("number") or 0) if isinstance(made, dict) else 0

    def branch_head(self, repo: str, branch: str) -> str:
        if not branch:
            return ""
        try:
            row = self._get(f"/repos/{repo}/branches/{branch}")
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(row, dict):
            return ""
        return str((row.get("commit") or {}).get("sha") or "")

    def file_at_ref(self, repo: str, path: str, ref: str) -> str:
        try:
            row = self._get(f"/repos/{repo}/contents/{path}", {"ref": ref})
        except Exception:  # noqa: BLE001
            # 404 is "the file is not there", which is the ordinary answer for
            # the optional files these callers read.
            return ""
        if not isinstance(row, dict):
            return ""
        if str(row.get("encoding") or "") == "base64":
            try:
                return base64.b64decode(row.get("content") or "").decode(
                    "utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return ""
        return str(row.get("content") or "")

    def comment_on_commit(self, repo: str, sha: str, body: str) -> bool:
        if not sha:
            return False
        return self._write("POST", f"/repos/{repo}/commits/{sha}/comments",
                           {"body": body or ""})


_VERDICTS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
}


def _repo_from_api_url(url: str) -> str:
    """`owner/name` out of an API URL that names a repository.

    Listings and search results name the repository only by its API URL, so
    this is the only way to find out which repository an item belongs to.
    """
    marker = "/repos/"
    text = str(url or "")
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].strip("/")


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


# Pipeline statuses, reduced the same way the check-runs are. `manual` and
# `scheduled` are failures for the same reason `action_required` is: they will
# not run on their own, so waiting for them is waiting forever.
_PIPELINE_PASSING = frozenset({"success", "skipped", "manual_success"})
_PIPELINE_FAILING = frozenset({"failed", "canceled", "cancelled", "manual",
                               "scheduled"})


class GitLabForge(Forge):
    """GitLab, over its REST API.

    Where the two hosts genuinely differ, this class carries the difference so
    that no caller has to:

      - there is no native close reason, so `close_issue` records the intent
        with the status label the model already defines and closes the issue;
      - a merge request has one pipeline rather than two independent check
        systems, so `checks_state` reduces one status instead of merging two;
      - a project is addressed by its URL-encoded path, not by `owner/name`.

    Methods this bot does not need from GitLab yet raise NotImplementedError
    naming what is missing. That is deliberate: an empty list would read to a
    caller as "there is nothing there", which is a different and much quieter
    kind of wrong.
    """

    kind = GITLAB
    change_request_noun = "merge request"

    def __init__(self, url: str, token: str, *, transport=_http):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.api = f"{self.url}/api/v4"
        self._transport = transport
        self._identity: str | None = None

    # -- transport ------------------------------------------------------

    def _headers(self) -> dict:
        return {"PRIVATE-TOKEN": self.token}

    @staticmethod
    def _project(repo: str) -> str:
        """A project path as the API addresses it.

        The path is URL-encoded whole, slashes included, because a GitLab
        project lives at `group/subgroup/name` and the API takes that as one
        path segment.
        """
        return urllib.parse.quote(str(repo or ""), safe="")

    def _get(self, path: str, params: dict | None = None):
        return self._transport("GET", f"{self.api}{path}",
                               headers=self._headers(), params=params)

    def _write(self, method: str, path: str, fields: dict | None = None):
        try:
            self._transport(method, f"{self.api}{path}",
                            headers=self._headers(), form_body=fields or {})
            return True
        except Exception:  # noqa: BLE001 - any failure is "did not write"
            return False

    # -- neutral shapes -------------------------------------------------

    def _issue(self, raw: dict, repo: str = "") -> dict:
        state = str(raw.get("state") or "opened").lower()
        # `opened` and `closed` are the two states; the vocabulary the rest of
        # this bot speaks is `open`.
        neutral_state = "closed" if state == "closed" else "open"
        closed_as = None
        if neutral_state == "closed":
            # There is no native close reason. The status label is where the
            # intent was written down when the issue was closed — see
            # close_issue.
            names = [str(l).strip().lower() for l in (raw.get("labels") or [])]
            revoked = any(n.endswith("wont-do") or n.endswith("won't do")
                          or n.endswith("wontdo") or n.endswith("duplicate")
                          for n in names)
            closed_as = REVOKED if revoked else DELIVERED
        return {
            "forge": self.kind,
            "repo": repo or _project_from_web_url(raw.get("web_url") or "",
                                                  self.url),
            "number": raw.get("iid"),
            "title": raw.get("title") or "",
            # The description is the body everywhere else in this bot, and the
            # destructive-wording guard reads it.
            "body": raw.get("description") or "",
            "url": raw.get("web_url") or "",
            "labels": [str(l) for l in (raw.get("labels") or []) if str(l).strip()],
            # Same field, this host's spelling of it. See the GitHub half.
            "author": ((raw.get("author") or {}).get("username") or ""),
            "state": neutral_state,
            "closedAs": closed_as,
            # Never true here: this host keeps merge requests in their own
            # collection, so an issue read is only ever an issue. Reported
            # anyway so callers ask the same question of both hosts.
            "isChangeRequest": False,
            "createdAt": raw.get("created_at") or "",
        }

    @staticmethod
    def _note(raw: dict) -> dict:
        return {
            "id": raw.get("id"),
            "body": raw.get("body") or "",
            "author": {"username": ((raw.get("author") or {}).get("username") or "")},
            "createdAt": raw.get("created_at") or "",
        }

    def _change_request(self, raw: dict, repo: str) -> dict:
        state = str(raw.get("state") or "").lower()
        neutral = {"opened": "open", "locked": "open",
                   "merged": "merged", "closed": "closed"}.get(state, state)
        return {
            "forge": self.kind,
            "repo": repo,
            "number": raw.get("iid"),
            "title": raw.get("title") or "",
            "body": raw.get("description") or "",
            "url": raw.get("web_url") or "",
            "state": neutral,
            # A draft is spelled in the title here, and also reported as a
            # field on recent versions. Read both, because a title-only draft
            # on an older instance would otherwise be reviewed early.
            "draft": bool(raw.get("draft") or raw.get("work_in_progress")
                          or str(raw.get("title") or "").lower().startswith("draft:")),
            "headSha": raw.get("sha") or "",
            "headRef": raw.get("source_branch") or "",
            "baseRef": raw.get("target_branch") or "",
            "labels": [str(l) for l in (raw.get("labels") or []) if str(l).strip()],
            "mergeable": (None if raw.get("merge_status") in (None, "checking",
                                                              "unchecked")
                          else raw.get("merge_status") == "can_be_merged"),
            "author": ((raw.get("author") or {}).get("username") or ""),
        }

    # -- identity -------------------------------------------------------

    def bot_identity(self) -> str:
        if self._identity is None:
            me = self._get("/user")
            self._identity = str((me or {}).get("username") or "") \
                if isinstance(me, dict) else ""
        return self._identity

    # -- discovery ------------------------------------------------------

    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Open work items assigned to the bot, keyed by project path.

        Both issues and TASKS are listed. Tasks share the issue iid space and
        every `/issues/:iid` endpoint, so a solver handles them identically —
        but some versions omit them from the default listing, which is why a
        second explicit pass is made and the two are de-duplicated.
        """
        by_repo: dict[str, list[dict]] = {}
        seen: set[tuple] = set()

        def collect(extra: dict) -> None:
            page = 1
            while True:
                batch = self._get("/issues", {
                    "scope": "assigned_to_me", "state": "opened",
                    "per_page": 100, "page": page, **extra,
                })
                rows = batch if isinstance(batch, list) else []
                for raw in rows:
                    key = (raw.get("project_id"), raw.get("iid"))
                    if key in seen:
                        continue
                    repo = _project_from_web_url(raw.get("web_url") or "",
                                                 self.url)
                    if not repo:
                        continue
                    seen.add(key)
                    by_repo.setdefault(repo, []).append(self._issue(raw, repo))
                if len(rows) < 100:
                    break
                page += 1

        collect({})
        collect({"issue_type": "task"})
        return by_repo

    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Open merge requests this bot should look at.

        AUTHORSHIP AND REVIEWER-SHIP ARE BOTH REAL SIGNALS HERE, which is the
        one place this differs from the other host: GitLab lets an author be
        a reviewer of their own merge request, so the bot's own work turns up
        under both scopes and the de-duplication is doing real work rather
        than guarding against a theoretical overlap.
        """
        login = self.bot_identity()
        if not login:
            return []
        seen: set[tuple[str, int]] = set()
        out: list[dict] = []
        for scope in ({"author_username": login},
                      {"reviewer_username": login}):
            rows = self._get("/merge_requests", {
                "state": "opened", "scope": "all",
                "order_by": "updated_at", "sort": "desc",
                "per_page": limit, **scope,
            })
            for raw in rows if isinstance(rows, list) else []:
                if not isinstance(raw, dict):
                    continue
                repo = _project_from_web_url(raw.get("web_url") or "", self.url)
                key = (repo, raw.get("iid"))
                if not repo or key in seen:
                    continue
                seen.add(key)
                out.append({
                    "forge": self.kind,
                    "repo": repo,
                    "number": raw.get("iid"),
                    "title": raw.get("title") or "",
                    "labels": [str(l) for l in (raw.get("labels") or [])],
                })
        return out[:limit]

    def accessible_repos(self, limit: int) -> list[str]:
        rows = self._get("/projects", {
            "membership": "true", "order_by": "last_activity_at",
            "sort": "desc", "per_page": limit, "simple": "true",
        })
        out = []
        for raw in rows if isinstance(rows, list) else []:
            path = raw.get("path_with_namespace") if isinstance(raw, dict) else None
            if path:
                out.append(str(path))
        return out[:limit]

    def default_branch_head(self, repo: str) -> tuple[str, str]:
        project = self._project(repo)
        try:
            raw = self._get(f"/projects/{project}")
        except Exception:  # noqa: BLE001
            return ("", "")
        branch = (raw or {}).get("default_branch") or "main"
        try:
            data = self._get(
                f"/projects/{project}/repository/branches/"
                f"{urllib.parse.quote(str(branch), safe='')}")
        except Exception:  # noqa: BLE001
            return (branch, "")
        sha = ((data.get("commit") or {}).get("id")) or ""
        return (branch, sha)

    # -- issues ---------------------------------------------------------

    def issue(self, repo: str, number: int) -> dict:
        try:
            raw = self._get(f"/projects/{self._project(repo)}/issues/{number}")
        except Exception:  # noqa: BLE001
            return {}
        return self._issue(raw, repo) if isinstance(raw, dict) else {}

    def comments(self, repo: str, number: int) -> list[dict]:
        """Notes on an issue, oldest first.

        System notes are dropped. GitLab records label changes, assignments
        and every other bookkeeping event as a note authored by whoever caused
        it, so leaving them in would make "a person replied" true the moment
        the bot itself added a label.
        """
        rows = self._get(f"/projects/{self._project(repo)}/issues/{number}/notes",
                         {"per_page": "100", "sort": "asc",
                          "order_by": "created_at"})
        if not isinstance(rows, list):
            raise ForgeError(f"unexpected notes payload for {repo}#{number}")
        return [self._note(r) for r in rows
                if isinstance(r, dict) and not r.get("system")]

    def post_comment(self, repo: str, number: int, body: str) -> bool:
        return self._write(
            "POST", f"/projects/{self._project(repo)}/issues/{number}/notes",
            {"body": body})

    def add_labels(self, repo: str, number: int, labels) -> bool:
        names = [str(l) for l in (labels or []) if str(l).strip()]
        if not names:
            return True
        return self._write(
            "PUT", f"/projects/{self._project(repo)}/issues/{number}",
            {"add_labels": ",".join(names)})

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        return self._write(
            "PUT", f"/projects/{self._project(repo)}/issues/{number}",
            {"remove_labels": str(label)})

    def ensure_label(self, repo: str, name: str, color: str = "",
                     description: str = "") -> bool:
        if not name:
            return False
        # A colour is a CSS hex here and six bare digits on the other host —
        # exactly the kind of difference no caller should have to know.
        fields = {"name": name,
                  "color": "#" + str(color or "c5def5").lstrip("#")}
        if description:
            fields["description"] = description
        try:
            self._transport("POST",
                            f"{self.api}/projects/{self._project(repo)}/labels",
                            headers=self._headers(), form_body=fields)
            return True
        except ForgeError as exc:
            # Conflict is "already defined", which is success here for the
            # same reason it is on the other host.
            return exc.code in (409, 400)
        except Exception:  # noqa: BLE001
            return False

    def close_issue(self, repo: str, number: int, delivered: bool) -> bool:
        """Close, recording the intent WITHOUT a native close reason.

        There is no field here that separates "shipped" from "called off", so
        the status label carries it: work that was called off is labelled
        before the close, and work that shipped is closed with the terminal
        labels cleared. The caller never learns any of that — it says which of
        the two happened and this decides how to write it down.
        """
        path = f"/projects/{self._project(repo)}/issues/{number}"
        fields = {"state_event": "close"}
        if delivered:
            fields["remove_labels"] = "status::wont-do,status::duplicate"
        else:
            fields["add_labels"] = "status::wont-do"
        return self._write("PUT", path, fields)

    # -- change requests ------------------------------------------------

    def open_change_requests_for_issue(self, repo: str,
                                       number: int) -> list[int]:
        rows = self._get(
            f"/projects/{self._project(repo)}/issues/{number}"
            "/related_merge_requests", {"per_page": "100"})
        out = []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("state") or "").lower() != "opened":
                continue
            if raw.get("iid"):
                out.append(raw["iid"])
        return out

    def change_request(self, repo: str, number: int) -> dict:
        try:
            raw = self._get(
                f"/projects/{self._project(repo)}/merge_requests/{number}")
        except Exception:  # noqa: BLE001
            return {}
        return self._change_request(raw, repo) if isinstance(raw, dict) else {}

    def change_request_comments(self, repo: str, number: int) -> list[dict]:
        rows = self._get(
            f"/projects/{self._project(repo)}/merge_requests/{number}/notes",
            {"per_page": "100", "sort": "asc", "order_by": "created_at"})
        if not isinstance(rows, list):
            raise ForgeError(
                f"unexpected notes payload for {repo}!{number}")
        return [self._note(r) for r in rows
                if isinstance(r, dict) and not r.get("system")]

    def checks_state(self, repo: str, sha: str) -> str:
        """The newest pipeline for one commit, reduced the same four ways.

        One pipeline, not two check systems — but the reduction has to land on
        the same four answers, or the callers would need to know which host
        they were gated by. A commit with no pipeline at all is `none`, never
        `pending`: nothing is going to arrive.
        """
        if not sha:
            return PENDING
        try:
            rows = self._get(f"/projects/{self._project(repo)}/pipelines", {
                "sha": sha, "per_page": "1",
                "order_by": "id", "sort": "desc",
            })
        except Exception:  # noqa: BLE001
            return PENDING
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        if not rows:
            return NONE
        return self.reduce_pipeline(str(rows[0].get("status") or ""))

    @staticmethod
    def reduce_pipeline(status: str) -> str:
        """One pipeline status → green / failed / pending / none."""
        state = str(status or "").strip().lower()
        if not state:
            return PENDING
        if state in _PIPELINE_FAILING:
            return FAILED
        if state in _PIPELINE_PASSING:
            return GREEN
        # `created`, `waiting_for_resource`, `preparing`, `pending`,
        # `running`, and anything a newer version invents. Unknown waits.
        return PENDING

    def checks(self, repo: str, sha: str) -> list[dict]:
        if not sha:
            return []
        try:
            pipelines = self._get(f"/projects/{self._project(repo)}/pipelines",
                                  {"sha": sha, "per_page": "1"})
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(pipelines, list) or not pipelines:
            return []
        pid = (pipelines[0] or {}).get("id")
        if not pid:
            return []
        try:
            jobs = self._get(
                f"/projects/{self._project(repo)}/pipelines/{pid}/jobs",
                {"per_page": "100"})
        except Exception:  # noqa: BLE001
            return []
        out = []
        for job in jobs if isinstance(jobs, list) else []:
            if not isinstance(job, dict):
                continue
            out.append({"name": job.get("name") or "",
                        "state": self.reduce_pipeline(
                            str(job.get("status") or ""))})
        return out

    def review_verdicts(self, repo: str, number: int) -> list[dict]:
        """Who approved this merge request.

        Approvals are the only structured verdict here — there is no
        "changes requested" state — so a rejection travels as an ordinary
        note and is read through `change_request_comments`.
        """
        try:
            raw = self._get(
                f"/projects/{self._project(repo)}/merge_requests/{number}"
                "/approvals")
        except Exception:  # noqa: BLE001
            return []
        sha = (raw or {}).get("sha") or ""
        out = []
        for who in (raw or {}).get("approved_by") or []:
            user = (who or {}).get("user") or {}
            out.append({
                "author": user.get("username") or "",
                "verdict": "approved",
                "body": "",
                "sha": sha,
            })
        return out

    def merge(self, repo: str, number: int, squash: bool = True,
              delete_branch: bool = True) -> bool:
        return self._write(
            "PUT",
            f"/projects/{self._project(repo)}/merge_requests/{number}/merge",
            {"squash": "true" if squash else "false",
             "should_remove_source_branch": "true" if delete_branch else "false"})

    def post_change_request_comment(self, repo: str, number: int,
                                    body: str) -> bool:
        """Merge request notes are their OWN endpoint here.

        `post_comment` writes to `/issues/<n>/notes`; handing it a merge
        request number would put the note on whatever issue happens to carry
        that iid.
        """
        return self._write(
            "POST",
            f"/projects/{self._project(repo)}/merge_requests/{number}/notes",
            {"body": body})

    def close_change_request(self, repo: str, number: int) -> bool:
        """`state_event` closes a merge request, as it closes an issue."""
        return self._write(
            "PUT",
            f"/projects/{self._project(repo)}/merge_requests/{number}",
            {"state_event": "close"})

    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete a branch, refusing the default one — see the GitHub twin.

        Protected branches are refused by the host as well, which is a second
        line rather than the first: this must not depend on somebody having
        remembered to protect the branch.
        """
        branch = str(branch or "").strip().lstrip("/")
        if not branch:
            return False
        try:
            if branch == (self.default_branch_head(repo) or ("", ""))[0]:
                return False
        except Exception:  # noqa: BLE001
            return False
        return self._write(
            "DELETE",
            f"/projects/{self._project(repo)}/repository/branches/"
            f"{urllib.parse.quote(branch, safe='')}")

    def security_findings(self, repo: str, number: int) -> list[dict]:
        raise NotImplementedError(
            "GitLab security findings are not read yet: they live behind the "
            "merge request's vulnerability report, which needs an Ultimate "
            "licence and a different endpoint from the one this bot uses "
            "elsewhere. Nothing calls this on GitLab today.")


    # -- the rest of what the runners ask -------------------------------

    def change_request_files(self, repo: str, number: int) -> list[dict]:
        row = self._get(
            f"/projects/{self._project(repo)}/merge_requests/{number}/changes")
        if not isinstance(row, dict):
            return []
        out = []
        for ch in row.get("changes") or []:
            if not isinstance(ch, dict):
                continue
            # No line counts are given here, so they are counted off the diff.
            # The alternative — reporting zero — would make every change look
            # like a rename to a caller deciding whether it is small.
            diff = str(ch.get("diff") or "")
            added = sum(1 for l in diff.splitlines()
                        if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff.splitlines()
                          if l.startswith("-") and not l.startswith("---"))
            status = "modified"
            if ch.get("new_file"):
                status = "added"
            elif ch.get("deleted_file"):
                status = "removed"
            elif ch.get("renamed_file"):
                status = "renamed"
            out.append({"path": ch.get("new_path") or ch.get("old_path") or "",
                        "added": added, "removed": removed, "status": status})
        return out

    def submit_review(self, repo: str, number: int, verdict: str,
                      body: str) -> bool:
        """Approval and the note that explains it, which are separate here.

        There is no single "submit a review with a verdict" call: approval is
        its own endpoint and carries no text, so the reasoning has to be a
        note. Both are attempted and the note is what the caller is told
        about, because the note is the part a person reads.
        """
        v = str(verdict or "").lower()
        path = f"/projects/{self._project(repo)}/merge_requests/{number}"
        if v == "approve":
            self._write("POST", f"{path}/approve")
        elif v == "request-changes":
            # Withdrawing an earlier approval is the closest thing to asking
            # for changes. It is best-effort: not having approved before is
            # not a failure to un-approve.
            self._write("POST", f"{path}/unapprove")
        elif v != "comment":
            return False
        if not body:
            return True
        return self._write("POST", f"{path}/notes", {"body": body})

    def review_requests(self, repo: str, number: int) -> list[str]:
        row = self._get(
            f"/projects/{self._project(repo)}/merge_requests/{number}")
        if not isinstance(row, dict):
            return []
        return [str((u or {}).get("username") or "")
                for u in (row.get("reviewers") or []) if isinstance(u, dict)]

    def request_review(self, repo: str, number: int, reviewers) -> bool:
        """Reviewers are set by numeric id here, so each name is looked up.

        A name that does not resolve is dropped rather than guessed at:
        sending an id for the wrong account would ask a stranger to review.
        """
        ids = []
        for name in (reviewers or []):
            name = str(name).strip()
            if not name:
                continue
            found = self._get("/users", {"username": name})
            if isinstance(found, list) and found and isinstance(found[0], dict):
                uid = found[0].get("id")
                if uid:
                    ids.append(uid)
        if not ids:
            return False
        return self._write(
            "PUT", f"/projects/{self._project(repo)}/merge_requests/{number}",
            {"reviewer_ids": ",".join(str(i) for i in ids)})

    def remove_review_request(self, repo: str, number: int,
                              reviewers) -> bool:
        # Reviewers are a SET here, not a collection to add to and remove
        # from, so withdrawing is writing an empty set. The names are ignored
        # deliberately: clearing is the only operation this host offers, and
        # pretending otherwise would silently keep whoever was not named.
        return self._write(
            "PUT", f"/projects/{self._project(repo)}/merge_requests/{number}",
            {"reviewer_ids": ""})

    def react(self, repo: str, number: int, comment_id, emoji: str) -> bool:
        # A note id is only addressable THROUGH the item it belongs to here,
        # which is why the interface asks for both.
        if not comment_id or not number:
            return False
        name = {"eyes": "eyes", "+1": "thumbsup", "-1": "thumbsdown"}.get(
            emoji or "eyes", emoji or "eyes")
        return self._write(
            "POST",
            f"/projects/{self._project(repo)}/issues/{number}"
            f"/notes/{comment_id}/award_emoji",
            {"name": name})

    def failing_check_log(self, repo: str, sha: str,
                          limit: int = 20000) -> str:
        if not sha:
            return ""
        try:
            pipelines = self._get(f"/projects/{self._project(repo)}/pipelines",
                                  {"sha": sha, "per_page": "1"})
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(pipelines, list) or not pipelines:
            return ""
        pid = (pipelines[0] or {}).get("id")
        if not pid:
            return ""
        try:
            jobs = self._get(
                f"/projects/{self._project(repo)}/pipelines/{pid}/jobs",
                {"scope[]": "failed", "per_page": "20"})
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(jobs, list) or not jobs:
            return ""
        job_id = (jobs[0] or {}).get("id")
        if not job_id:
            return ""
        try:
            text = self._transport(
                "GET",
                f"{self.api}/projects/{self._project(repo)}/jobs/{job_id}/trace",
                headers=self._headers(), raw=True)
        except Exception:  # noqa: BLE001
            return ""
        text = text if isinstance(text, str) else ""
        return text[-limit:] if limit and len(text) > limit else text

    def recent_change_requests(self, repo: str, state: str = "closed",
                               limit: int = 50) -> list[dict]:
        # `closed` means something narrower here — a merge request that was
        # abandoned — so the caller's "closed" has to ask for merged ones too,
        # or the delivery sweep finds nothing it was looking for.
        wanted = "all" if state == "closed" else "opened"
        rows = self._get(
            f"/projects/{self._project(repo)}/merge_requests",
            {"state": wanted, "order_by": "updated_at", "sort": "desc",
             "per_page": str(limit)})
        if not isinstance(rows, list):
            return []
        return [self._change_request(r, repo) for r in rows
                if isinstance(r, dict)]

    def open_issues(self, repo: str, limit: int = 100) -> list[dict]:
        rows = self._get(f"/projects/{self._project(repo)}/issues",
                         {"state": "opened", "per_page": str(limit)})
        if not isinstance(rows, list):
            return []
        return [self._issue(r, repo) for r in rows if isinstance(r, dict)]

    def create_issue(self, repo: str, title: str, body: str = "",
                     labels=None, assignees=None) -> int:
        fields = {"title": title, "description": body or ""}
        names = [str(l) for l in (labels or []) if str(l).strip()]
        if names:
            fields["labels"] = ",".join(names)
        # Assignees are numeric ids here, so each account name is resolved.
        # A name that does not resolve is dropped rather than guessed at: an
        # id for the wrong account assigns somebody else's work to a stranger.
        ids = []
        for who in (assignees or []):
            who = str(who).strip()
            if not who:
                continue
            found = self._get("/users", {"username": who})
            if isinstance(found, list) and found and isinstance(found[0], dict):
                if found[0].get("id"):
                    ids.append(found[0]["id"])
        if ids:
            fields["assignee_ids"] = ",".join(str(i) for i in ids)
        try:
            made = self._transport(
                "POST", f"{self.api}/projects/{self._project(repo)}/issues",
                headers=self._headers(), form_body=fields)
        except Exception:  # noqa: BLE001
            return 0
        return int((made or {}).get("iid") or 0) if isinstance(made, dict) else 0

    def branch_head(self, repo: str, branch: str) -> str:
        if not branch:
            return ""
        try:
            row = self._get(
                f"/projects/{self._project(repo)}/repository/branches/"
                f"{urllib.parse.quote(str(branch), safe='')}")
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(row, dict):
            return ""
        return str((row.get("commit") or {}).get("id") or "")

    def file_at_ref(self, repo: str, path: str, ref: str) -> str:
        try:
            text = self._transport(
                "GET",
                f"{self.api}/projects/{self._project(repo)}/repository/files/"
                f"{urllib.parse.quote(str(path), safe='')}/raw",
                headers=self._headers(), params={"ref": ref})
        except Exception:  # noqa: BLE001
            return ""
        # The raw endpoint answers with the file, not with JSON, so the shared
        # reader hands back None for anything it could not parse — which for a
        # file that is not JSON is every file.
        return text if isinstance(text, str) else ""

    def comment_on_commit(self, repo: str, sha: str, body: str) -> bool:
        if not sha:
            return False
        return self._write(
            "POST",
            f"/projects/{self._project(repo)}/repository/commits/{sha}/comments",
            {"note": body or ""})


def _project_from_web_url(web_url: str, base: str) -> str:
    """The project path out of a browser URL.

    Work items are served under `/-/work_items/<iid>` on newer versions and
    `/-/issues/<iid>` on older ones, and merge requests under
    `/-/merge_requests/<iid>`. Anything else is not ours to interpret, and
    guessing would attach an item to the wrong project.
    """
    prefix = f"{str(base or '').rstrip('/')}/"
    text = str(web_url or "")
    if not prefix or not text.startswith(prefix):
        return ""
    rest = text[len(prefix):]
    for marker in ("/-/issues/", "/-/work_items/", "/-/merge_requests/"):
        if marker in rest:
            return rest.split(marker)[0]
    return ""


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


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
    return Forges(out)

"""GitHub, behind the forge interface.

Split out of a single 3,000-line module when a third host arrived.
A FLAT sibling of forge.py rather than a package member: a ConfigMap
key cannot contain a slash, and that ConfigMap is how builder code
reaches a pod without rebuilding the image.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse

from forge import (  # noqa: F401 - the shared vocabulary
    DELIVERED, FAILED, GREEN, NONE, PENDING, REVOKED,
    Forge, ForgeError, RateLimited, _http, _with_credential,
    GITHUB,
)



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

    def _is_user(self, name: str) -> bool:
        """Does this name belong to ONE person account?

        An organisation answers no — the endpoint serves both and says which.
        So does a team (`org/team`), which is not an account at all, and a
        name that cannot be read: an unreadable name is not proof that it is
        safe to notify everybody behind it.
        """
        cached = self._mention_seen(name)
        if cached is not None:
            return cached
        ok = False
        if "/" not in name:
            try:
                who = self._get(f"/users/{name}")
            except Exception:  # noqa: BLE001 - unreadable is "not a person"
                who = None
            ok = bool(isinstance(who, dict)
                      and str(who.get("type") or "") == "User")
        return self._mention_seen(name, ok)

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

    def owner_login(self, repo: str) -> str:
        """The account the repository belongs to, when that is a PERSON.

        `owner/name` names a user for a personal repository and an
        ORGANISATION for everything else, and the two read identically in the
        path — which is why this asks the host instead of splitting a string.
        An organisation cannot be assigned an issue, and @-mentioning one
        notifies every member of it.

        For an organisation-owned repository the answer is one admin
        collaborator, alphabetically so the same tick answers the same way
        twice, and never the bot.
        """
        bot = (self.bot_identity() or "").lower()
        try:
            raw = self._get(f"/repos/{repo}")
        except Exception:  # noqa: BLE001 - unreadable is "cannot name one"
            return ""
        owner = (raw or {}).get("owner") or {} if isinstance(raw, dict) else {}
        login = str(owner.get("login") or "")
        kind = str(owner.get("type") or "User")
        if login and kind == "User" and login.lower() != bot:
            return login
        try:
            people = self._get(f"/repos/{repo}/collaborators",
                               {"permission": "admin", "per_page": "100"})
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(people, list):
            return ""
        names = sorted(str(p.get("login") or "") for p in people
                       if isinstance(p, dict)
                       and str(p.get("login") or "").strip()
                       and str(p.get("login") or "").lower() != bot)
        return names[0] if names else ""

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

    def clone_url(self, repo: str) -> str:
        # Only GitHub keeps the API on a different host from the git remote,
        # so the web host is derived rather than stored: `api.github.com` for
        # the public instance, `<host>/api/v3` for an Enterprise one.
        base = self.api
        if base.endswith("/api/v3"):
            base = base[: -len("/api/v3")]
        elif "//api.github.com" in base:
            base = base.replace("//api.github.com", "//github.com")
        # `x-access-token` is the user half GitHub accepts for both a PAT and
        # an app installation token.
        return _with_credential(base, "x-access-token", self.token, repo)

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
                           {"body": self._one_human_only(body)})

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
                           {"body": self._one_human_only(body)})

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
                           {"event": event,
                            "body": self._one_human_only(body or "")})

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
        #
        # ONE reviewer, whatever the caller passed: a review asked of a crowd
        # is a review nobody owns, and every one of them is notified.
        return self._write("POST",
                           f"/repos/{repo}/pulls/{number}/requested_reviewers",
                           {"reviewers": names[:1]})

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
        payload = {"title": title, "body": self._one_human_only(body or "")}
        names = [str(l) for l in (labels or []) if str(l).strip()]
        if names:
            payload["labels"] = names
        # ONE assignee. Work handed to a crowd belongs to nobody, and the
        # caller that hands over a list is the caller that took a group for a
        # person — see Forge.owner_login.
        who = [str(a) for a in (assignees or []) if str(a).strip()]
        if who:
            payload["assignees"] = who[:1]
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

"""Gitea, behind the forge interface.

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
    Forge, ForgeError, RateLimited, _http,
    GITEA,
)



# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Gitea
# ---------------------------------------------------------------------------


class GiteaForge(Forge):
    """Gitea, over its REST API (`/api/v1`).

    Gitea's payloads are GitHub-shaped — `number`, `body`, `html_url`, labels
    as objects, `user.login` — so this reads closer to `GitHubForge` than to
    `GitLabForge`. It deliberately does NOT subclass it. The two drift, and
    the places they already differ are exactly the places a shared parent
    would hide:

      - there is no `state_reason`, so a close intent is recorded the way
        GitLab records it: with the status label, read back in `_issue`;
      - a label is removed BY NUMERIC ID, not by name, so `remove_label` has
        to look the name up first;
      - `create_issue` takes label IDs only, while adding labels to an issue
        that already exists accepts names — so the same word means two things
        depending on the call, and both are handled here;
      - there is no code-scanning surface and no commit-comment endpoint at
        all, so two methods raise rather than answer;
      - CI arrives as commit statuses, and job logs live behind Actions, so
        `failing_check_log` walks run -> job -> log instead of asking once.

    Methods this bot cannot answer from Gitea raise NotImplementedError naming
    what is missing, for the reason the whole module gives: an empty list
    reads to a caller as "there is nothing there", which is a different and
    much quieter kind of wrong.
    """

    kind = GITEA
    change_request_noun = "pull request"

    def __init__(self, url: str, token: str, *, transport=_http):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.api = f"{self.url}/api/v1"
        self._transport = transport
        self._identity: str | None = None

    # -- transport ------------------------------------------------------

    def _headers(self) -> dict:
        # "API tokens must be prepended with 'token' followed by a space."
        # The `access_token` query parameter also authenticates and is
        # deprecated for removal; a token in a URL lands in access logs, so
        # the header is the only form used here.
        return {"Authorization": f"token {self.token}",
                "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None, *, raw: bool = False):
        return self._transport("GET", f"{self.api}{path}",
                               headers=self._headers(), params=params, raw=raw)

    def _write(self, method: str, path: str, payload=None):
        """A write that reports rather than raises — see the GitHub half."""
        try:
            self._transport(method, f"{self.api}{path}",
                            headers=self._headers(), json_body=payload)
            return True
        except Exception:  # noqa: BLE001 - any failure is "did not write"
            return False

    @staticmethod
    def _split(repo: str) -> tuple[str, str]:
        """`owner/name`. Addressed by path segments, not URL-encoded as one."""
        owner, _, name = (repo or "").partition("/")
        return owner, name

    def _repo_path(self, repo: str) -> str:
        owner, name = self._split(repo)
        return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"

    # -- identity -------------------------------------------------------

    def bot_identity(self) -> str:
        if self._identity is None:
            me = self._get("/user")
            self._identity = str((me or {}).get("login") or "") \
                if isinstance(me, dict) else ""
        return self._identity

    def _is_user(self, name: str) -> bool:
        """One person, or something that stands for several.

        An organisation is an account here too, and a team (`org/team`) is not
        an account at all. The organisation endpoint knows only organisations,
        so a name it recognises is not a person — and a name that cannot be
        read is treated the same way, because an unreadable name is not proof
        that it is safe to notify everybody behind it.
        """
        cached = self._mention_seen(name)
        if cached is not None:
            return cached
        if "/" in name:
            return self._mention_seen(name, False)
        try:
            org = self._get(f"/orgs/{urllib.parse.quote(name)}")
        except Exception:  # noqa: BLE001 - not an organisation, or unreadable
            org = None
        if isinstance(org, dict) and org.get("id"):
            return self._mention_seen(name, False)
        try:
            who = self._get(f"/users/{urllib.parse.quote(name)}")
        except Exception:  # noqa: BLE001
            who = None
        ok = bool(isinstance(who, dict) and who.get("id"))
        return self._mention_seen(name, ok)

    def owner_login(self, repo: str) -> str:
        """The account the repository belongs to, when that is a PERSON.

        This host models an organisation as an account too, so `owner/name`
        alone cannot say whether the first segment is somebody or a team — and
        handing work to a team hands it to everyone in it. The organisation
        endpoint answers the question: it knows only organisations, so a
        repository whose owner it recognises is an org-owned one, and the
        human is one of the collaborators instead (alphabetically, never the
        bot, so two ticks name the same person).
        """
        bot = (self.bot_identity() or "").lower()
        owner, _name = self._split(repo)
        owner = str(owner or "").strip()
        if not owner:
            return ""
        try:
            org = self._get(f"/orgs/{urllib.parse.quote(owner)}")
        except Exception:  # noqa: BLE001 - not an organisation, or unreadable
            org = None
        if not isinstance(org, dict) or not org.get("id"):
            return "" if owner.lower() == bot else owner
        try:
            people = self._get(f"{self._repo_path(repo)}/collaborators",
                               {"limit": "100"})
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
        """Label names. Objects here, but bare strings are accepted for the
        same reason the GitHub half accepts them: search payloads vary."""
        out = []
        for item in raw or []:
            name = item.get("name", "") if isinstance(item, dict) else item
            name = str(name or "").strip()
            if name:
                out.append(name)
        return out

    def _issue(self, raw: dict, repo: str = "") -> dict:
        state = str(raw.get("state") or "open").lower()
        neutral_state = "closed" if state == "closed" else "open"
        closed_as = None
        if neutral_state == "closed":
            # No native close reason on this host. The status label is where
            # the intent was written down when the issue was closed — the
            # same convention as GitLab, and read back the same way.
            names = [n.lower() for n in self._labels(raw.get("labels"))]
            revoked = any(n.endswith("wont-do") or n.endswith("won't do")
                          or n.endswith("wontdo") or n.endswith("duplicate")
                          for n in names)
            closed_as = REVOKED if revoked else DELIVERED
        return {
            "forge": self.kind,
            "repo": repo or self._repo_from_raw(raw),
            "number": raw.get("number"),
            "title": raw.get("title") or "",
            "body": raw.get("body") or "",
            "url": raw.get("html_url") or "",
            "labels": self._labels(raw.get("labels")),
            "state": neutral_state,
            "closedAs": closed_as,
            "author": ((raw.get("user") or {}).get("login") or ""),
            # Same coincidence as GitHub: a pull request served from the
            # issues collection carries this extra key and is otherwise
            # indistinguishable from an issue.
            "isChangeRequest": bool(raw.get("pull_request")),
            "createdAt": raw.get("created_at") or "",
        }

    @staticmethod
    def _repo_from_raw(raw: dict) -> str:
        """`owner/name` from whatever the payload carried it in.

        Issue payloads embed the whole repository object; search results embed
        a smaller one. Both spell the answer `full_name`.
        """
        repo = raw.get("repository") or {}
        full = str(repo.get("full_name") or "").strip()
        if full:
            return full
        owner = str((repo.get("owner") or {}).get("login") or "").strip()
        name = str(repo.get("name") or "").strip()
        return f"{owner}/{name}" if owner and name else ""

    @staticmethod
    def _note(raw: dict) -> dict:
        return {
            "id": raw.get("id"),
            "body": raw.get("body") or "",
            "author": {"username": ((raw.get("user") or {}).get("login") or "")},
            "createdAt": raw.get("created_at") or "",
        }

    def _change_request(self, raw: dict, repo: str) -> dict:
        head = raw.get("head") or {}
        base = raw.get("base") or {}
        state = str(raw.get("state") or "").lower()
        neutral = "open" if state == "open" else "closed"
        # `merged` is the flag; `state` stays "closed" for a landed pull
        # request, so reading state alone would report every merge as an
        # abandonment.
        if raw.get("merged") or raw.get("merged_at"):
            neutral = "merged"
        return {
            "forge": self.kind,
            "repo": repo,
            "number": raw.get("number"),
            "title": raw.get("title") or "",
            "body": raw.get("body") or "",
            "url": raw.get("html_url") or "",
            "state": neutral,
            "draft": bool(raw.get("draft")),
            "headSha": head.get("sha") or "",
            "headRef": head.get("ref") or "",
            "baseRef": base.get("ref") or "",
            "labels": self._labels(raw.get("labels")),
            "mergeable": raw.get("mergeable"),
            "author": ((raw.get("user") or {}).get("login") or ""),
        }

    # -- discovery ------------------------------------------------------

    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Every open issue assigned to the bot, in one cross-repository call
        per page.

        Pull requests come back from the same search and are dropped, for the
        reason the GitHub half drops them: planning one as an issue would
        spawn a solver on the bot's own branch.
        """
        by_repo: dict[str, list[dict]] = {}
        page = 1
        while True:
            batch = self._get("/repos/issues/search",
                              {"assigned": "true", "state": "open",
                               "type": "issues", "limit": 100, "page": page})
            batch = batch if isinstance(batch, list) else []
            for raw in batch:
                if raw.get("pull_request"):
                    continue
                issue = self._issue(raw)
                repo = issue.get("repo") or ""
                if repo:
                    by_repo.setdefault(repo, []).append(issue)
            if len(batch) < 100:
                break
            page += 1
        return by_repo

    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Open pull requests to look at, newest first.

        Stubs, like the other hosts': the search collection does not carry a
        head commit, and `change_request` is what fills that in.
        """
        out: list[dict] = []
        batch = self._get("/repos/issues/search",
                          {"state": "open", "type": "pulls",
                           "limit": max(1, int(limit or 1)),
                           "sort": "updated", "order": "desc"})
        for raw in (batch if isinstance(batch, list) else []):
            repo = self._repo_from_raw(raw)
            if not repo:
                continue
            out.append({
                "forge": self.kind,
                "repo": repo,
                "number": raw.get("number"),
                "title": raw.get("title") or "",
                "labels": self._labels(raw.get("labels")),
                "author": ((raw.get("user") or {}).get("login") or ""),
            })
        return out[:limit]

    def accessible_repos(self, limit: int) -> list[str]:
        out: list[str] = []
        page = 1
        while len(out) < limit:
            batch = self._get("/user/repos", {"limit": 50, "page": page})
            batch = batch if isinstance(batch, list) else []
            if not batch:
                break
            for raw in batch:
                full = str(raw.get("full_name") or "").strip()
                if full:
                    out.append(full)
            if len(batch) < 50:
                break
            page += 1
        return out[:limit]

    def default_branch_head(self, repo: str) -> tuple[str, str]:
        try:
            meta = self._get(self._repo_path(repo))
        except Exception:  # noqa: BLE001 - unreadable is "skip this repo"
            return "", ""
        branch = str((meta or {}).get("default_branch") or "")
        if not branch:
            return "", ""
        return branch, self.branch_head(repo, branch)

    # -- issues ---------------------------------------------------------

    def issue(self, repo: str, number: int) -> dict:
        try:
            raw = self._get(f"{self._repo_path(repo)}/issues/{int(number)}")
        except Exception:  # noqa: BLE001
            return {}
        return self._issue(raw, repo) if isinstance(raw, dict) else {}

    def comments(self, repo: str, number: int) -> list[dict]:
        try:
            raw = self._get(
                f"{self._repo_path(repo)}/issues/{int(number)}/comments")
        except Exception:  # noqa: BLE001
            return []
        return [self._note(c) for c in (raw if isinstance(raw, list) else [])]

    def post_comment(self, repo: str, number: int, body: str) -> bool:
        return self._write(
            "POST", f"{self._repo_path(repo)}/issues/{int(number)}/comments",
            {"body": self._one_human_only(body or "")})

    def add_labels(self, repo: str, number: int, labels) -> bool:
        names = [str(l).strip() for l in (labels or []) if str(l).strip()]
        if not names:
            return True
        # This endpoint takes "a list of integers representing label IDs or a
        # list of strings representing label names" — names are what the bot
        # has, so names are what it sends. `create_issue` is NOT the same:
        # see the note there.
        return self._write(
            "POST", f"{self._repo_path(repo)}/issues/{int(number)}/labels",
            {"labels": names})

    def _label_id(self, repo: str, name: str) -> int:
        """The numeric id of a label, or 0 when the repository has no such
        label. Removing one is by id here, and only by id."""
        target = str(name or "").strip().lower()
        if not target:
            return 0
        page = 1
        while True:
            try:
                batch = self._get(f"{self._repo_path(repo)}/labels",
                                  {"limit": 100, "page": page})
            except Exception:  # noqa: BLE001
                return 0
            batch = batch if isinstance(batch, list) else []
            for raw in batch:
                if str(raw.get("name") or "").strip().lower() == target:
                    try:
                        return int(raw.get("id") or 0)
                    except (TypeError, ValueError):
                        return 0
            if len(batch) < 100:
                return 0
            page += 1

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        """Take one label off an issue.

        BY ID, not by name — the delete endpoint's path parameter is an
        integer. A name posted there addresses no label and the host answers
        404, which reads like "the issue is gone" and is not.
        """
        label_id = self._label_id(repo, label)
        if not label_id:
            # Not defined in this repository, so it is not on the issue
            # either. Nothing to remove is not a failure.
            return True
        return self._write(
            "DELETE",
            f"{self._repo_path(repo)}/issues/{int(number)}/labels/{label_id}")

    def ensure_label(self, repo: str, name: str, color: str = "",
                     description: str = "") -> bool:
        """Define a label in a repository. Already existing is SUCCESS."""
        if not str(name or "").strip():
            return False
        if self._label_id(repo, name):
            return True
        payload = {"name": str(name).strip(),
                   "color": (color or "ededed").lstrip("#"),
                   "description": description or ""}
        if self._write("POST", f"{self._repo_path(repo)}/labels", payload):
            return True
        # A racing writer may have defined it between the read and the write.
        return bool(self._label_id(repo, name))

    def close_issue(self, repo: str, number: int, delivered: bool) -> bool:
        """Close an issue, recording whether the work shipped.

        No native close reason on this host, so the intent goes where GitLab
        puts it — on the status label — and `_issue` reads it back.
        """
        if delivered:
            self.remove_label(repo, number, "status::wont-do")
            self.remove_label(repo, number, "status::duplicate")
        else:
            self.add_labels(repo, number, ["status::wont-do"])
        return self._write(
            "PATCH", f"{self._repo_path(repo)}/issues/{int(number)}",
            {"state": "closed"})

    # -- change requests ------------------------------------------------

    def open_change_requests_for_issue(self, repo: str,
                                       number: int) -> list[int]:
        """Open pull requests that would close this issue.

        There is no "what closes this" endpoint, so the open ones are read and
        their bodies searched for a closing keyword — the same information a
        person reads off the pull request page.
        """
        out: list[int] = []
        try:
            batch = self._get(f"{self._repo_path(repo)}/pulls",
                              {"state": "open", "limit": 100})
        except Exception:  # noqa: BLE001
            return []
        pattern = re.compile(
            r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[^\n#]*#%d\b"
            % int(number), re.I)
        for raw in (batch if isinstance(batch, list) else []):
            if pattern.search(str(raw.get("body") or "")):
                try:
                    out.append(int(raw.get("number")))
                except (TypeError, ValueError):
                    continue
        return out

    def change_request(self, repo: str, number: int) -> dict:
        try:
            raw = self._get(f"{self._repo_path(repo)}/pulls/{int(number)}")
        except Exception:  # noqa: BLE001
            return {}
        return self._change_request(raw, repo) if isinstance(raw, dict) else {}

    def change_request_comments(self, repo: str, number: int) -> list[dict]:
        """Notes on a pull request.

        A pull request and an issue share one comment collection here, as they
        do on GitHub — but this stays a separate method for the reason the ABC
        gives: on a host where they do not share, the same number is a
        different item entirely.
        """
        return self.comments(repo, number)

    # -- checks ---------------------------------------------------------

    @staticmethod
    def _reduce_status(state: str) -> str:
        """One commit-status state, in this bot's four."""
        s = str(state or "").strip().lower()
        if s == "success":
            return GREEN
        if s in ("failure", "error"):
            return FAILED
        if s in ("pending", "running", "waiting", "blocked"):
            return PENDING
        # Anything unrecognised is pending. An unknown CI state must never
        # read as green.
        return PENDING

    def checks_state(self, repo: str, sha: str) -> str:
        """CI for one commit, reduced to green / failed / pending / none.

        CI reaches this host as commit statuses — both from Actions and from
        anything external — so unlike GitHub there is no second collection to
        merge. The combined endpoint already reduces, but it is re-derived
        here from the individual statuses so that this method and `checks`
        cannot disagree.
        """
        states = [c["state"] for c in self.checks(repo, sha)]
        if not states:
            # No CI configured at all is not a build in progress.
            return NONE
        if any(s == FAILED for s in states):
            return FAILED
        if any(s == PENDING for s in states):
            return PENDING
        return GREEN

    def checks(self, repo: str, sha: str) -> list[dict]:
        """Each status on a commit as {name, state}.

        The same reduction `checks_state` uses, so a check that reads failed
        in one cannot read pending in the other. Only the newest status per
        context counts: this host appends rather than replaces, so a context
        that failed and was re-run keeps both rows.
        """
        try:
            raw = self._get(f"{self._repo_path(repo)}/commits/"
                            f"{urllib.parse.quote(str(sha))}/statuses",
                            {"limit": 100})
        except Exception:  # noqa: BLE001
            return []
        newest: dict[str, dict] = {}
        for item in (raw if isinstance(raw, list) else []):
            context = str(item.get("context") or "").strip() or "status"
            created = str(item.get("created_at") or "")
            prev = newest.get(context)
            if prev is None or created >= prev["created"]:
                newest[context] = {"created": created,
                                   "state": item.get("status")}
        return [{"name": name, "state": self._reduce_status(v["state"])}
                for name, v in sorted(newest.items())]

    def review_verdicts(self, repo: str, number: int) -> list[dict]:
        try:
            raw = self._get(
                f"{self._repo_path(repo)}/pulls/{int(number)}/reviews")
        except Exception:  # noqa: BLE001
            return []
        mapping = {"APPROVED": "approved",
                   "REQUEST_CHANGES": "changes_requested",
                   "COMMENT": "commented"}
        out = []
        for item in (raw if isinstance(raw, list) else []):
            if item.get("dismissed"):
                verdict = "dismissed"
            else:
                verdict = mapping.get(
                    str(item.get("state") or "").upper(), "commented")
            out.append({
                "author": ((item.get("user") or {}).get("login") or ""),
                "verdict": verdict,
                "body": item.get("body") or "",
                "sha": item.get("commit_id") or "",
            })
        return out

    def merge(self, repo: str, number: int, squash: bool = True,
              delete_branch: bool = True) -> bool:
        return self._write(
            "POST", f"{self._repo_path(repo)}/pulls/{int(number)}/merge",
            {"do": "squash" if squash else "merge",
             "delete_branch_after_merge": bool(delete_branch)})

    def security_findings(self, repo: str, number: int) -> list[dict]:
        raise NotImplementedError(
            "Gitea has no code-scanning API: there is no endpoint that "
            "reports findings the host itself raised against a change.")

    def post_change_request_comment(self, repo: str, number: int,
                                    body: str) -> bool:
        """Say something on the pull request.

        Lands on the shared issue/pull comment collection, which is the same
        call as `post_comment` HERE and is deliberately still its own method
        — see the ABC.
        """
        return self._write(
            "POST", f"{self._repo_path(repo)}/issues/{int(number)}/comments",
            {"body": self._one_human_only(body or "")})

    def close_change_request(self, repo: str, number: int) -> bool:
        """Close a pull request without merging. The branch survives."""
        return self._write(
            "PATCH", f"{self._repo_path(repo)}/pulls/{int(number)}",
            {"state": "closed"})

    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete a branch, refusing the default branch."""
        name = str(branch or "").strip()
        if not name:
            return False
        try:
            meta = self._get(self._repo_path(repo))
        except Exception:  # noqa: BLE001
            return False
        if name == str((meta or {}).get("default_branch") or ""):
            # Refused here rather than trusted to the caller: this is the one
            # irreversible call in the interface.
            return False
        return self._write(
            "DELETE",
            f"{self._repo_path(repo)}/branches/{urllib.parse.quote(name)}")

    def change_request_files(self, repo: str, number: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            try:
                batch = self._get(
                    f"{self._repo_path(repo)}/pulls/{int(number)}/files",
                    {"limit": 100, "page": page})
            except Exception:  # noqa: BLE001
                return out
            batch = batch if isinstance(batch, list) else []
            for raw in batch:
                out.append({
                    "path": raw.get("filename") or "",
                    "added": int(raw.get("additions") or 0),
                    "removed": int(raw.get("deletions") or 0),
                    "status": raw.get("status") or "",
                })
            if len(batch) < 100:
                return out
            page += 1

    def submit_review(self, repo: str, number: int, verdict: str,
                      body: str) -> bool:
        """Leave a review. Refusal on one's own pull request is NORMAL."""
        event = {"approve": "APPROVED",
                 "request-changes": "REQUEST_CHANGES",
                 "comment": "COMMENT"}.get(
                     str(verdict or "").strip().lower(), "COMMENT")
        return self._write(
            "POST", f"{self._repo_path(repo)}/pulls/{int(number)}/reviews",
            {"event": event, "body": self._one_human_only(body or "")})

    def review_requests(self, repo: str, number: int) -> list[str]:
        """Who has been asked to review, by account name.

        Carried on the pull request itself rather than by its own endpoint.
        """
        try:
            raw = self._get(f"{self._repo_path(repo)}/pulls/{int(number)}")
        except Exception:  # noqa: BLE001
            return []
        out = []
        for user in ((raw or {}).get("requested_reviewers") or []):
            login = str((user or {}).get("login") or "").strip()
            if login:
                out.append(login)
        return out

    def request_review(self, repo: str, number: int, reviewers) -> bool:
        names = [str(r).strip() for r in (reviewers or []) if str(r).strip()]
        if not names:
            return True
        return self._write(
            "POST",
            f"{self._repo_path(repo)}/pulls/{int(number)}/requested_reviewers",
            # ONE reviewer: a review asked of a crowd is a review nobody owns.
            {"reviewers": names[:1]})

    def remove_review_request(self, repo: str, number: int,
                              reviewers) -> bool:
        names = [str(r).strip() for r in (reviewers or []) if str(r).strip()]
        if not names:
            return True
        return self._write(
            "DELETE",
            f"{self._repo_path(repo)}/pulls/{int(number)}/requested_reviewers",
            # ONE reviewer: a review asked of a crowd is a review nobody owns.
            {"reviewers": names[:1]})

    def react(self, repo: str, number: int, comment_id, emoji: str) -> bool:
        """Acknowledge one comment.

        Addressed globally by comment id here, so `number` is accepted and
        unused — the ABC takes both so that either host can answer.
        """
        try:
            cid = int(comment_id)
        except (TypeError, ValueError):
            return False
        return self._write(
            "POST",
            f"{self._repo_path(repo)}/issues/comments/{cid}/reactions",
            {"content": emoji or "+1"})

    def failing_check_log(self, repo: str, sha: str,
                          limit: int = 20000) -> str:
        """The output of the first failing job on a commit, or "".

        Three hops, because this host keeps CI results and CI logs in
        different places: the runs for a commit, then that run's jobs, then
        the failing job's log. Truncated from the END — a failure says what
        went wrong on its last lines.
        """
        base = self._repo_path(repo)
        try:
            runs = self._get(f"{base}/actions/runs",
                             {"head_sha": str(sha), "limit": 20})
        except Exception:  # noqa: BLE001
            return ""
        entries = runs.get("workflow_runs") if isinstance(runs, dict) else runs
        for run in (entries if isinstance(entries, list) else []):
            if str(run.get("conclusion") or "").lower() not in (
                    "failure", "cancelled", "canceled", "timed_out"):
                continue
            try:
                jobs = self._get(f"{base}/actions/runs/{int(run.get('id'))}/jobs",
                                 {"limit": 50})
            except Exception:  # noqa: BLE001
                continue
            job_list = jobs.get("jobs") if isinstance(jobs, dict) else jobs
            for job in (job_list if isinstance(job_list, list) else []):
                if str(job.get("conclusion") or "").lower() not in (
                        "failure", "cancelled", "canceled", "timed_out"):
                    continue
                try:
                    log = self._get(
                        f"{base}/actions/jobs/{int(job.get('id'))}/logs",
                        raw=True)
                except Exception:  # noqa: BLE001
                    return ""
                text = log if isinstance(log, str) else ""
                return text[-limit:] if limit and len(text) > limit else text
        return ""

    def recent_change_requests(self, repo: str, state: str = "closed",
                               limit: int = 50) -> list[dict]:
        try:
            raw = self._get(f"{self._repo_path(repo)}/pulls",
                            {"state": state or "closed", "sort": "recentupdate",
                             "limit": max(1, int(limit or 1))})
        except Exception:  # noqa: BLE001
            return []
        return [self._change_request(item, repo)
                for item in (raw if isinstance(raw, list) else [])][:limit]

    def open_issues(self, repo: str, limit: int = 100) -> list[dict]:
        """Every open issue in one repository.

        Pull requests are excluded by asking for issues only — this host
        serves both from the collection, and a caller planning work on a pull
        request would be planning the bot's own output.
        """
        try:
            raw = self._get(f"{self._repo_path(repo)}/issues",
                            {"state": "open", "type": "issues",
                             "limit": max(1, int(limit or 1))})
        except Exception:  # noqa: BLE001
            return []
        out = []
        for item in (raw if isinstance(raw, list) else []):
            if item.get("pull_request"):
                continue
            out.append(self._issue(item, repo))
        return out[:limit]

    def create_issue(self, repo: str, title: str, body: str = "",
                     labels=None, assignees=None) -> int:
        """Open an issue. Returns its number, or 0.

        `labels` here is LABEL IDS ONLY — unlike adding labels to an issue
        that already exists, which accepts names. The names the bot has are
        resolved to ids first, and a name this repository has never defined is
        created rather than dropped: silently losing the status label is how
        an issue ends up outside the vocabulary every planner reads.
        """
        payload: dict = {"title": title or "",
                         "body": self._one_human_only(body or "")}
        names = [str(l).strip() for l in (labels or []) if str(l).strip()]
        if names:
            ids = []
            for name in names:
                label_id = self._label_id(repo, name)
                if not label_id and self.ensure_label(repo, name):
                    label_id = self._label_id(repo, name)
                if label_id:
                    ids.append(label_id)
            if ids:
                payload["labels"] = ids
        # ONE assignee, whatever the caller passed. The interface promises one
        # human (Forge.owner_login); a list here is a caller that mistook a
        # team for a person, and it hands the work to all of them.
        who = [str(a).strip() for a in (assignees or []) if str(a).strip()]
        if who:
            payload["assignees"] = who[:1]
        try:
            raw = self._transport(
                "POST", f"{self.api}{self._repo_path(repo)}/issues",
                headers=self._headers(), json_body=payload)
        except Exception:  # noqa: BLE001
            return 0
        try:
            return int((raw or {}).get("number") or 0)
        except (TypeError, ValueError):
            return 0

    def branch_head(self, repo: str, branch: str) -> str:
        try:
            raw = self._get(f"{self._repo_path(repo)}/branches/"
                            f"{urllib.parse.quote(str(branch))}")
        except Exception:  # noqa: BLE001
            return ""
        return str(((raw or {}).get("commit") or {}).get("id") or "")

    def file_at_ref(self, repo: str, path: str, ref: str) -> str:
        """One file's text at one commit, or "" when it is not there."""
        try:
            raw = self._get(
                f"{self._repo_path(repo)}/contents/"
                f"{urllib.parse.quote(str(path))}", {"ref": ref})
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(raw, dict):
            return ""
        if str(raw.get("encoding") or "") == "base64":
            try:
                return base64.b64decode(
                    raw.get("content") or "").decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return ""
        return str(raw.get("content") or "")

    def comment_on_commit(self, repo: str, sha: str, body: str) -> bool:
        raise NotImplementedError(
            "Gitea has no commit-comment API: a note can be attached to an "
            "issue or a pull request, but not to a commit on its own.")

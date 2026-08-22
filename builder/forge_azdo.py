"""Azure DevOps Services, behind the forge interface.

The furthest of the four hosts from GitHub's model, and the mapping is most of
the work:

  * THERE ARE NO ISSUES. There are WORK ITEMS, in a different service
    (`/_apis/wit/`) from Git (`/_apis/git/`), typed Bug / Task / User Story.
    The body is `System.Description`, labels are `System.Tags` (one
    semicolon-joined string), and a comment is a patch to `System.History`.
  * A WORK ITEM BELONGS TO A PROJECT, NOT A REPOSITORY. Every other host keys
    issues by the repository they live in because that is where they live.
    Here it has to be derived — see `_repo_for_work_item`.
  * THE ORGANISATION IS CONFIGURATION, like GitLab's URL, so the neutral
    `repo` stays two segments: `project/repo`. A repository name is unique
    only inside its project; the org-unique handle is a GUID, which is what
    every Git call is addressed by and why `_repo_index` exists.
  * EVERY REQUEST NEEDS `api-version`. Omit it and the request is rejected
    with a message about the version, not about what was asked.
  * THE PAT IS SENT AS BASIC AUTH WITH AN EMPTY USERNAME. A bearer header
    401s, which reads exactly like a bad token and is not.
  * A REVIEW IS A NUMBER. Reviewers hold a vote: 10 approved, 5 approved with
    suggestions, 0 no vote, -5 waiting for the author, -10 rejected.

A FLAT sibling of forge.py rather than a package member: a ConfigMap key
cannot contain a slash, and that ConfigMap is how builder code reaches a pod
without rebuilding the image.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from forge import (  # noqa: F401 - the shared vocabulary
    DELIVERED, FAILED, GREEN, NONE, PENDING, REVOKED,
    Forge, ForgeError, RateLimited, _http,
    AZDO,
)

# Required on every call. Not a default anyone may rely on: a request without
# it is rejected outright.
API_VERSION = "7.1"

# What a reviewer's vote means. The numbers are the API's, not ours.
VOTE_APPROVED = 10
VOTE_APPROVED_WITH_SUGGESTIONS = 5
VOTE_NO_VOTE = 0
VOTE_WAITING = -5
VOTE_REJECTED = -10

# A commit status state, reduced to this bot's four. `notApplicable` is not a
# gate at all and is dropped rather than counted — counting it as pending
# would leave a repository that publishes one waiting forever.
_STATUS_PASSING = frozenset({"succeeded"})
_STATUS_FAILING = frozenset({"failed", "error"})
_STATUS_IGNORED = frozenset({"notapplicable"})

# The artifact links a work item carries when it has been worked. The repo
# GUID is the second field of the vstfs path, and the whole path is URL
# encoded inside the link.
_ARTIFACT = re.compile(
    r"vstfs:///Git/(?:PullRequestId|Ref|Commit)/"
    r"([0-9a-fA-F-]{36})%2F([0-9a-fA-F-]{36})", re.I)


class AzureDevOpsForge(Forge):
    """Azure DevOps Services (cloud), over its REST API."""

    kind = AZDO
    change_request_noun = "pull request"

    def __init__(self, org_url: str, token: str, *, transport=_http):
        self.url = (org_url or "").rstrip("/")
        self.token = token or ""
        self.api = f"{self.url}/_apis"
        self._transport = transport
        self._identity: str | None = None
        self._repos: dict | None = None      # guid -> "project/repo", and back

    # -- transport ------------------------------------------------------

    def _headers(self, content_type: str = "application/json") -> dict:
        # A PAT goes in Basic auth with an EMPTY USERNAME. `Bearer <pat>`
        # 401s, which reads like a bad token and is not one.
        import base64
        raw = base64.b64encode(f":{self.token}".encode()).decode()
        return {"Authorization": f"Basic {raw}",
                "Accept": "application/json",
                "Content-Type": content_type}

    def _get(self, path: str, params: dict | None = None, *, raw: bool = False):
        p = dict(params or {})
        p["api-version"] = API_VERSION
        return self._transport("GET", f"{self.url}{path}",
                               headers=self._headers(), params=p, raw=raw)

    def _write(self, method: str, path: str, payload=None, *,
               content_type: str = "application/json"):
        """A write that reports rather than raises — see the GitHub half."""
        try:
            self._transport(method, f"{self.url}{path}",
                            headers=self._headers(content_type),
                            params={"api-version": API_VERSION},
                            json_body=payload)
            return True
        except Exception:  # noqa: BLE001 - any failure is "did not write"
            return False

    def _patch_fields(self, project: str, work_item: int, ops: list) -> bool:
        """A work item edit, which is a JSON PATCH and not an object.

        The content type is load-bearing: this endpoint rejects
        `application/json` outright, and the message names the media type
        rather than the field that was wrong.
        """
        return self._write(
            "PATCH",
            f"/{urllib.parse.quote(project)}/_apis/wit/workitems/{int(work_item)}",
            ops, content_type="application/json-patch+json")

    # -- identity -------------------------------------------------------

    def bot_identity(self) -> str:
        if self._identity is None:
            # Not under /_apis on this host: the profile service is its own.
            try:
                me = self._transport(
                    "GET", "https://app.vssps.visualstudio.com/_apis/profile/"
                           "profiles/me",
                    headers=self._headers(),
                    params={"api-version": API_VERSION})
            except Exception:  # noqa: BLE001
                me = None
            self._identity = str((me or {}).get("displayName") or "") \
                if isinstance(me, dict) else ""
        return self._identity

    # -- addressing -----------------------------------------------------

    @staticmethod
    def _split(repo: str) -> tuple[str, str]:
        """`project/repo`. The organisation is configuration, not part of it."""
        project, _, name = (repo or "").partition("/")
        return project, name

    def _repo_index(self) -> dict:
        """Both directions of `project/repo` <-> repository GUID.

        Built once. Every Git call on this host is addressed by the GUID, and
        a repository name is unique only inside its project — so the name a
        caller uses cannot be put in a URL without this.
        """
        if self._repos is None:
            index: dict = {}
            try:
                listing = self._get("/_apis/git/repositories",
                                    {"includeLinks": "false"})
            except Exception:  # noqa: BLE001
                self._repos = {}
                return self._repos
            for raw in ((listing or {}).get("value") or []):
                guid = str(raw.get("id") or "")
                project = str((raw.get("project") or {}).get("name") or "")
                name = str(raw.get("name") or "")
                if not (guid and project and name):
                    continue
                index[guid.lower()] = f"{project}/{name}"
                index[f"{project}/{name}"] = guid
            self._repos = index
        return self._repos

    def _repo_id(self, repo: str) -> str:
        return str(self._repo_index().get(repo, ""))

    def _git(self, repo: str, suffix: str = "") -> str:
        """The Git path for a repository, or "" when it is not addressable."""
        project, _ = self._split(repo)
        guid = self._repo_id(repo)
        if not (project and guid):
            return ""
        return (f"/{urllib.parse.quote(project)}/_apis/git/repositories/"
                f"{guid}{suffix}")

    def _projects(self) -> list[str]:
        """Every project these credentials can see."""
        out = []
        for key in self._repo_index():
            if "/" in key:
                project = key.split("/", 1)[0]
                if project not in out:
                    out.append(project)
        return out

    def _repos_in(self, project: str) -> list[str]:
        return sorted(k for k in self._repo_index()
                      if "/" in k and k.split("/", 1)[0] == project)

    def _repo_for_work_item(self, raw: dict, project: str) -> str:
        """Which repository a work item is about.

        A work item belongs to a PROJECT here, so this cannot be read off it
        the way every other host reads it off an issue. Two answers, in order,
        and a deliberate refusal rather than a guess:

          1. the repository named in its own Git artifact links — a branch, a
             commit or a pull request it is already attached to. This is the
             real answer whenever the work has been started.
          2. failing that, the project's ONLY repository, when it has exactly
             one. Unambiguous by construction.

        A work item with no link in a project with several repositories is
        SKIPPED, and skipped visibly. Picking one would put a solver to work
        on the wrong repository, which is worse than not working it at all.
        """
        index = self._repo_index()
        for relation in (raw.get("relations") or []):
            found = _ARTIFACT.search(str(relation.get("url") or ""))
            if found:
                mapped = index.get(found.group(2).lower())
                if mapped:
                    return mapped
        candidates = self._repos_in(project)
        return candidates[0] if len(candidates) == 1 else ""

    # -- neutral shapes -------------------------------------------------

    @staticmethod
    def _tags(raw: dict) -> list[str]:
        """Labels. One semicolon-joined string on this host, not a list."""
        fields = raw.get("fields") or {}
        text = str(fields.get("System.Tags") or "")
        return [t.strip() for t in text.split(";") if t.strip()]

    def _issue(self, raw: dict, repo: str = "") -> dict:
        fields = raw.get("fields") or {}
        state = str(fields.get("System.State") or "").strip()
        # State names are the PROCESS TEMPLATE's, not the API's: Agile closes
        # to "Closed", Scrum to "Done", Basic to "Done" as well, and a
        # customised process can spell it anything. `System.State` cannot be
        # compared against a fixed word, so the CATEGORY is what decides —
        # and when that is absent, anything not obviously open is closed.
        category = str(fields.get("System.StateCategory") or "").strip().lower()
        if category:
            closed = category in ("completed", "removed")
        else:
            closed = state.lower() in ("closed", "done", "resolved", "removed")
        labels = self._tags(raw)
        closed_as = None
        if closed:
            # No native "was it delivered" flag that survives every process,
            # so the status label carries it — the convention GitLab and
            # Gitea already use here.
            low = [n.lower() for n in labels]
            revoked = (category == "removed"
                       or state.lower() == "removed"
                       or any(n.endswith("wont-do") or n.endswith("wontdo")
                              or n.endswith("duplicate") for n in low))
            closed_as = REVOKED if revoked else DELIVERED
        assigned = fields.get("System.AssignedTo") or {}
        created_by = fields.get("System.CreatedBy") or {}
        return {
            "forge": self.kind,
            "repo": repo,
            "number": raw.get("id"),
            "title": fields.get("System.Title") or "",
            # The body everywhere else in this bot. The destructive-wording
            # guard reads it, so getting this wrong disarms the guard.
            "body": fields.get("System.Description") or "",
            "url": (((raw.get("_links") or {}).get("html") or {}).get("href")
                    or ""),
            "labels": labels,
            "state": "closed" if closed else "open",
            "closedAs": closed_as,
            "author": (created_by.get("uniqueName")
                       or created_by.get("displayName") or ""),
            # Work items and pull requests are separate collections here, so
            # an item read is only ever a work item.
            "isChangeRequest": False,
            "createdAt": fields.get("System.CreatedDate") or "",
            "assignee": (assigned.get("uniqueName")
                         or assigned.get("displayName") or ""),
        }

    @staticmethod
    def _note(raw: dict) -> dict:
        author = raw.get("createdBy") or raw.get("author") or {}
        return {
            "id": raw.get("id"),
            # Work item comments call it `text`; pull request comments call
            # it `content`. Both are "what was said".
            "body": raw.get("text") or raw.get("content") or "",
            "author": {"username": (author.get("uniqueName")
                                    or author.get("displayName") or "")},
            "createdAt": (raw.get("createdDate")
                          or raw.get("publishedDate") or ""),
        }

    def _change_request(self, raw: dict, repo: str) -> dict:
        status = str(raw.get("status") or "").lower()
        # `completed` is this host's word for merged. `abandoned` is closed
        # without landing; reading either as the other inverts the answer the
        # delivery sweep depends on.
        state = {"active": "open", "completed": "merged",
                 "abandoned": "closed"}.get(status, status or "open")
        created_by = raw.get("createdBy") or {}
        merge_status = str(raw.get("mergeStatus") or "").lower()
        return {
            "forge": self.kind,
            "repo": repo,
            "number": raw.get("pullRequestId"),
            "title": raw.get("title") or "",
            "body": raw.get("description") or "",
            "url": (((raw.get("_links") or {}).get("web") or {}).get("href")
                    or ""),
            "state": state,
            "draft": bool(raw.get("isDraft")),
            "headSha": ((raw.get("lastMergeSourceCommit") or {})
                        .get("commitId") or ""),
            "headRef": _short_ref(raw.get("sourceRefName")),
            "baseRef": _short_ref(raw.get("targetRefName")),
            "labels": [str(l.get("name") or "") for l in (raw.get("labels") or [])
                       if str(l.get("name") or "").strip()],
            # `succeeded` is the only value that means it would merge; the
            # rest are conflicts, policy refusals or "not computed yet".
            "mergeable": True if merge_status == "succeeded" else (
                False if merge_status in ("conflicts", "rejectedbypolicy",
                                          "failure") else None),
            "author": (created_by.get("uniqueName")
                       or created_by.get("displayName") or ""),
        }

    # -- discovery ------------------------------------------------------

    def assigned_open_issues(self) -> dict[str, list[dict]]:
        """Every open work item assigned to the bot, keyed by `project/repo`.

        A WIQL query per project rather than one across the organisation: the
        published spec marks `project` required on this route even though the
        documentation's own sample omits it, and the project is needed anyway
        to resolve a work item to a repository.
        """
        by_repo: dict[str, list[dict]] = {}
        for project in self._projects():
            wiql = ("SELECT [System.Id] FROM WorkItems "
                    "WHERE [System.AssignedTo] = @Me "
                    "AND [System.State] NOT IN ('Closed','Done','Removed','Resolved') "
                    "ORDER BY [System.ChangedDate] DESC")
            try:
                result = self._transport(
                    "POST",
                    f"{self.url}/{urllib.parse.quote(project)}/_apis/wit/wiql",
                    headers=self._headers(),
                    params={"api-version": API_VERSION, "$top": 200},
                    json_body={"query": wiql})
            except Exception:  # noqa: BLE001 - a project we cannot query is skipped
                continue
            ids = [str(w.get("id")) for w in ((result or {}).get("workItems") or [])
                   if w.get("id") is not None]
            for raw in self._work_items(project, ids):
                repo = self._repo_for_work_item(raw, project)
                if not repo:
                    # Deliberately dropped rather than guessed at — see
                    # _repo_for_work_item.
                    continue
                by_repo.setdefault(repo, []).append(self._issue(raw, repo))
        return by_repo

    def _work_items(self, project: str, ids: list) -> list[dict]:
        """Full work items for a batch of ids, relations included.

        Relations are what `_repo_for_work_item` reads, and they are NOT in
        the default projection — asking without $expand returns items that
        all look unlinked.
        """
        out: list[dict] = []
        base = f"/{urllib.parse.quote(project)}/_apis/wit/workitems"
        for start in range(0, len(ids), 200):     # the documented batch cap
            chunk = ids[start:start + 200]
            if not chunk:
                continue
            try:
                got = self._get(base, {"ids": ",".join(chunk),
                                       "$expand": "relations"})
            except Exception:  # noqa: BLE001
                continue
            out.extend((got or {}).get("value") or [])
        return out

    def reviewable_change_requests(self, limit: int) -> list[dict]:
        """Open pull requests to look at, newest first.

        Stubs, like the other hosts': the listing carries no head commit and
        `change_request` is what fills that in.
        """
        out: list[dict] = []
        for repo in sorted(k for k in self._repo_index() if "/" in k):
            path = self._git(repo, "/pullrequests")
            if not path:
                continue
            try:
                got = self._get(path, {"searchCriteria.status": "active",
                                       "$top": max(1, int(limit or 1))})
            except Exception:  # noqa: BLE001
                continue
            for raw in ((got or {}).get("value") or []):
                created_by = raw.get("createdBy") or {}
                out.append({
                    "forge": self.kind,
                    "repo": repo,
                    "number": raw.get("pullRequestId"),
                    "title": raw.get("title") or "",
                    "labels": [str(l.get("name") or "")
                               for l in (raw.get("labels") or [])],
                    "author": (created_by.get("uniqueName")
                               or created_by.get("displayName") or ""),
                })
            if len(out) >= limit:
                break
        return out[:limit]

    def accessible_repos(self, limit: int) -> list[str]:
        return sorted(k for k in self._repo_index() if "/" in k)[:limit]

    def default_branch_head(self, repo: str) -> tuple[str, str]:
        path = self._git(repo)
        if not path:
            return "", ""
        try:
            meta = self._get(path)
        except Exception:  # noqa: BLE001
            return "", ""
        branch = _short_ref((meta or {}).get("defaultBranch"))
        if not branch:
            return "", ""
        return branch, self.branch_head(repo, branch)

    # -- issues (work items) --------------------------------------------

    def issue(self, repo: str, number: int) -> dict:
        project, _ = self._split(repo)
        try:
            raw = self._get(
                f"/{urllib.parse.quote(project)}/_apis/wit/workitems/{int(number)}",
                {"$expand": "relations"})
        except Exception:  # noqa: BLE001
            return {}
        return self._issue(raw, repo) if isinstance(raw, dict) else {}

    def comments(self, repo: str, number: int) -> list[dict]:
        project, _ = self._split(repo)
        try:
            raw = self._get(f"/{urllib.parse.quote(project)}/_apis/wit/"
                            f"workItems/{int(number)}/comments")
        except Exception:  # noqa: BLE001
            return []
        return [self._note(c) for c in ((raw or {}).get("comments") or [])]

    def post_comment(self, repo: str, number: int, body: str) -> bool:
        """Say something on a work item.

        Written as a patch to `System.History` rather than to the comments
        collection: the history entry is what shows in the work item's
        Discussion, and it is the one route that does not depend on the
        comments API being enabled for the process.
        """
        project, _ = self._split(repo)
        return self._patch_fields(project, number, [
            {"op": "add", "path": "/fields/System.History",
             "value": body or ""}])

    def add_labels(self, repo: str, number: int, labels) -> bool:
        """Add tags. They are ONE semicolon-joined string, so this is a
        read-modify-write and not an append."""
        names = [str(l).strip() for l in (labels or []) if str(l).strip()]
        if not names:
            return True
        current = self.issue(repo, number).get("labels") or []
        merged = list(current)
        for name in names:
            if name.lower() not in [c.lower() for c in merged]:
                merged.append(name)
        project, _ = self._split(repo)
        return self._patch_fields(project, number, [
            {"op": "add", "path": "/fields/System.Tags",
             "value": "; ".join(merged)}])

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        target = str(label or "").strip().lower()
        if not target:
            return False
        current = self.issue(repo, number).get("labels") or []
        kept = [c for c in current if c.lower() != target]
        if len(kept) == len(current):
            return True                      # not there is not a failure
        project, _ = self._split(repo)
        return self._patch_fields(project, number, [
            {"op": "add", "path": "/fields/System.Tags",
             "value": "; ".join(kept)}])

    def ensure_label(self, repo: str, name: str, color: str = "",
                     description: str = "") -> bool:
        """A no-op that reports success.

        Tags on this host are created by being used — there is no definition
        to make first, and nothing to colour. Answering False would read to
        every caller as "labelling is broken here" and stop them labelling
        anything.
        """
        return bool(str(name or "").strip())

    def close_issue(self, repo: str, number: int, delivered: bool) -> bool:
        """Close a work item, recording whether the work shipped.

        The state name is the process template's — Agile says Closed, Scrum
        says Done — so the intent goes on the status label, which is readable
        under every process, and the state is set to whichever of the two the
        project accepts.
        """
        if delivered:
            self.remove_label(repo, number, "status::wont-do")
            self.remove_label(repo, number, "status::duplicate")
        else:
            self.add_labels(repo, number, ["status::wont-do"])
        project, _ = self._split(repo)
        for state in ("Closed", "Done", "Resolved"):
            if self._patch_fields(project, number, [
                    {"op": "add", "path": "/fields/System.State",
                     "value": state}]):
                return True
        return False

    # -- change requests ------------------------------------------------

    def open_change_requests_for_issue(self, repo: str,
                                       number: int) -> list[int]:
        """Open pull requests attached to this work item.

        Read off the WORK ITEM's artifact links rather than searched for in
        the pull requests: attaching a pull request to a work item is a first
        class relation here, so there is no body convention to parse.
        """
        project, _ = self._split(repo)
        try:
            raw = self._get(
                f"/{urllib.parse.quote(project)}/_apis/wit/workitems/{int(number)}",
                {"$expand": "relations"})
        except Exception:  # noqa: BLE001
            return []
        out: list[int] = []
        for relation in ((raw or {}).get("relations") or []):
            url = str(relation.get("url") or "")
            if "PullRequestId" not in url:
                continue
            tail = url.rsplit("%2F", 1)[-1]
            try:
                pr = int(tail)
            except (TypeError, ValueError):
                continue
            if self.change_request(repo, pr).get("state") == "open":
                out.append(pr)
        return out

    def change_request(self, repo: str, number: int) -> dict:
        path = self._git(repo, f"/pullrequests/{int(number)}")
        if not path:
            return {}
        try:
            raw = self._get(path)
        except Exception:  # noqa: BLE001
            return {}
        return self._change_request(raw, repo) if isinstance(raw, dict) else {}

    def change_request_comments(self, repo: str, number: int) -> list[dict]:
        """Notes on a pull request.

        Comments live inside THREADS here, so this flattens them. System
        threads — "x added a reviewer" — are dropped: they are not anybody
        saying anything, and a guard that reads them as replies would think a
        person had answered.
        """
        path = self._git(repo, f"/pullRequests/{int(number)}/threads")
        if not path:
            return []
        try:
            raw = self._get(path)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for thread in ((raw or {}).get("value") or []):
            for comment in (thread.get("comments") or []):
                if str(comment.get("commentType") or "").lower() == "system":
                    continue
                out.append(self._note(comment))
        return out

    # -- checks ---------------------------------------------------------

    @staticmethod
    def _reduce_status(state: str) -> str:
        s = str(state or "").strip().lower()
        if s in _STATUS_PASSING:
            return GREEN
        if s in _STATUS_FAILING:
            return FAILED
        # `notSet` and `pending` are both "no answer yet", and anything
        # unrecognised joins them. An unknown CI state must never read green.
        return PENDING

    def checks(self, repo: str, sha: str) -> list[dict]:
        """Each status on a commit as {name, state}.

        `notApplicable` is DROPPED rather than reduced: it means the check
        deliberately does not apply to this commit, so counting it as pending
        would leave the gate waiting on something that will never answer.
        """
        path = self._git(repo, f"/commits/{urllib.parse.quote(str(sha))}/statuses")
        if not path:
            return []
        try:
            raw = self._get(path, {"$top": 100, "latestOnly": "true"})
        except Exception:  # noqa: BLE001
            return []
        out = []
        for item in ((raw or {}).get("value") or []):
            state = str(item.get("state") or "")
            if state.strip().lower() in _STATUS_IGNORED:
                continue
            context = item.get("context") or {}
            name = "/".join(p for p in (context.get("genre"),
                                        context.get("name")) if p) or "status"
            out.append({"name": name, "state": self._reduce_status(state)})
        return sorted(out, key=lambda c: c["name"])

    def checks_state(self, repo: str, sha: str) -> str:
        states = [c["state"] for c in self.checks(repo, sha)]
        if not states:
            return NONE
        if any(s == FAILED for s in states):
            return FAILED
        if any(s == PENDING for s in states):
            return PENDING
        return GREEN

    def review_verdicts(self, repo: str, number: int) -> list[dict]:
        """Reviews on a pull request.

        A review is a NUMBER here — the reviewer's vote — not an event with a
        body, so there is no text to carry and no ordering by time. A vote of
        0 is "has not voted", which is not a verdict and is dropped.
        """
        path = self._git(repo, f"/pullrequests/{int(number)}")
        if not path:
            return []
        try:
            raw = self._get(path)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for reviewer in ((raw or {}).get("reviewers") or []):
            try:
                vote = int(reviewer.get("vote") or 0)
            except (TypeError, ValueError):
                vote = 0
            if vote == VOTE_NO_VOTE:
                continue
            if vote >= VOTE_APPROVED_WITH_SUGGESTIONS:
                verdict = "approved"
            elif vote <= VOTE_WAITING:
                verdict = "changes_requested"
            else:
                verdict = "commented"
            out.append({
                "author": (reviewer.get("uniqueName")
                           or reviewer.get("displayName") or ""),
                "verdict": verdict,
                "body": "",
                "sha": "",
            })
        return out

    def merge(self, repo: str, number: int, squash: bool = True,
              delete_branch: bool = True) -> bool:
        """Complete a pull request.

        `lastMergeSourceCommit` is REQUIRED and is not optimistic locking to
        be skipped: without it the completion is rejected, and with a stale
        one it is refused rather than merging the wrong tree.
        """
        current = self.change_request(repo, number)
        head = current.get("headSha") or ""
        if not head:
            return False
        path = self._git(repo, f"/pullrequests/{int(number)}")
        if not path:
            return False
        return self._write("PATCH", path, {
            "status": "completed",
            "lastMergeSourceCommit": {"commitId": head},
            "completionOptions": {
                "deleteSourceBranch": bool(delete_branch),
                "mergeStrategy": "squash" if squash else "noFastForward",
            },
        })

    def security_findings(self, repo: str, number: int) -> list[dict]:
        raise NotImplementedError(
            "Azure DevOps has no code-scanning API on the Git service: "
            "Advanced Security findings are a separate paid product with its "
            "own surface, and nothing here reports findings the host raised "
            "against a change.")

    def post_change_request_comment(self, repo: str, number: int,
                                    body: str) -> bool:
        """Say something on the pull request, as a new thread."""
        path = self._git(repo, f"/pullRequests/{int(number)}/threads")
        if not path:
            return False
        return self._write("POST", path, {
            "comments": [{"content": body or "", "commentType": "text"}],
            "status": "active",
        })

    def close_change_request(self, repo: str, number: int) -> bool:
        """Abandon a pull request. The branch survives."""
        path = self._git(repo, f"/pullrequests/{int(number)}")
        if not path:
            return False
        return self._write("PATCH", path, {"status": "abandoned"})

    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete a branch, refusing the default branch.

        Deleting is an UPDATE to an all-zero object id here, and it needs the
        current tip as `oldObjectId` — which is also what stops it deleting a
        branch that moved since it was read.
        """
        name = str(branch or "").strip()
        if not name:
            return False
        default, _ = self.default_branch_head(repo)
        if name == default:
            return False
        head = self.branch_head(repo, name)
        if not head:
            return False
        path = self._git(repo, "/refs")
        if not path:
            return False
        return self._write("POST", path, [{
            "name": f"refs/heads/{name}",
            "oldObjectId": head,
            "newObjectId": "0" * 40,
        }])

    def change_request_files(self, repo: str, number: int) -> list[dict]:
        """What a change touches.

        Two calls: the newest iteration, then its changes. Line counts are
        not reported by this endpoint at all, so `added` and `removed` are
        zero rather than invented — a caller sizing a change gets the file
        count, which is the part this host can answer honestly.
        """
        base = self._git(repo, f"/pullRequests/{int(number)}/iterations")
        if not base:
            return []
        try:
            iterations = self._get(base)
        except Exception:  # noqa: BLE001
            return []
        entries = (iterations or {}).get("value") or []
        if not entries:
            return []
        newest = entries[-1].get("id")
        try:
            changes = self._get(f"{base}/{int(newest)}/changes", {"$top": 1000})
        except Exception:  # noqa: BLE001
            return []
        out = []
        for change in ((changes or {}).get("changeEntries") or []):
            item = change.get("item") or {}
            if item.get("isFolder"):
                continue
            out.append({
                "path": str(item.get("path") or "").lstrip("/"),
                "added": 0,
                "removed": 0,
                "status": str(change.get("changeType") or ""),
            })
        return out

    def _reviewer_id(self, repo: str, number: int, who: str) -> str:
        """A reviewer's identity id on one pull request.

        Reviewers are addressed by identity GUID, not by account name, and
        there is no lookup from one to the other on this route — so the id
        comes from the pull request's own reviewer list.
        """
        target = str(who or "").strip().lower()
        path = self._git(repo, f"/pullrequests/{int(number)}")
        if not (target and path):
            return ""
        try:
            raw = self._get(path)
        except Exception:  # noqa: BLE001
            return ""
        for reviewer in ((raw or {}).get("reviewers") or []):
            names = [str(reviewer.get(k) or "").lower()
                     for k in ("uniqueName", "displayName", "id")]
            if target in names:
                return str(reviewer.get("id") or "")
        return ""

    def submit_review(self, repo: str, number: int, verdict: str,
                      body: str) -> bool:
        """Leave a review: a VOTE, plus the text as a thread.

        The vote is the whole review on this host — there is no body on it —
        so the text is posted separately. The comment is what the merge gate
        reads on every host, so it is written FIRST: a vote that lands
        without its explanation is worse than an explanation without a vote.
        """
        said = self.post_change_request_comment(repo, number, body) if body else True
        vote = {"approve": VOTE_APPROVED,
                "request-changes": VOTE_REJECTED,
                "comment": VOTE_NO_VOTE}.get(
                    str(verdict or "").strip().lower(), VOTE_NO_VOTE)
        me = self._reviewer_id(repo, number, self.bot_identity())
        if not me:
            # Not a reviewer on this pull request, so there is no vote to
            # cast. The comment still landed, and that is the verdict the
            # gate reads.
            return said
        path = self._git(repo, f"/pullRequests/{int(number)}/reviewers/{me}")
        if not path:
            return said
        return self._write("PUT", path, {"vote": vote}) and said

    def review_requests(self, repo: str, number: int) -> list[str]:
        path = self._git(repo, f"/pullrequests/{int(number)}")
        if not path:
            return []
        try:
            raw = self._get(path)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for reviewer in ((raw or {}).get("reviewers") or []):
            name = str(reviewer.get("uniqueName")
                       or reviewer.get("displayName") or "").strip()
            if name:
                out.append(name)
        return out

    def request_review(self, repo: str, number: int, reviewers) -> bool:
        """Ask accounts to review.

        Adding a reviewer needs their identity GUID, and resolving a name to
        one is a different service (`vssps` identities) that a code-scoped PAT
        cannot always read. An account already ON the pull request is
        addressable, so this refuses honestly rather than half-working.
        """
        names = [str(r).strip() for r in (reviewers or []) if str(r).strip()]
        if not names:
            return True
        ok = True
        for name in names:
            who = self._reviewer_id(repo, number, name)
            if not who:
                ok = False
                continue
            path = self._git(repo, f"/pullRequests/{int(number)}/reviewers/{who}")
            ok = self._write("PUT", path, {"vote": VOTE_NO_VOTE}) and ok
        return ok

    def remove_review_request(self, repo: str, number: int,
                              reviewers) -> bool:
        names = [str(r).strip() for r in (reviewers or []) if str(r).strip()]
        if not names:
            return True
        ok = True
        for name in names:
            who = self._reviewer_id(repo, number, name)
            if not who:
                continue                     # not asked is not a failure
            path = self._git(repo, f"/pullRequests/{int(number)}/reviewers/{who}")
            ok = self._write("DELETE", path) and ok
        return ok

    def react(self, repo: str, number: int, comment_id, emoji: str) -> bool:
        raise NotImplementedError(
            "Azure DevOps has no reaction API for pull request or work item "
            "comments: a comment can be replied to, but not acknowledged "
            "without saying something.")

    def failing_check_log(self, repo: str, sha: str,
                          limit: int = 20000) -> str:
        """The output of the first failing build on a commit, or "".

        Statuses carry a `targetUrl` to whatever produced them, which may be
        anything at all, so the build is found through the Build service by
        source version instead. Truncated from the END — a failure says what
        went wrong on its last lines.
        """
        project, _ = self._split(repo)
        guid = self._repo_id(repo)
        if not (project and guid):
            return ""
        base = f"/{urllib.parse.quote(project)}/_apis/build"
        try:
            builds = self._get(f"{base}/builds", {
                "repositoryId": guid, "repositoryType": "TfsGit",
                "resultFilter": "failed,canceled", "$top": 5,
                "queryOrder": "finishTimeDescending"})
        except Exception:  # noqa: BLE001
            return ""
        for build in ((builds or {}).get("value") or []):
            if str(build.get("sourceVersion") or "") != str(sha):
                continue
            try:
                log = self._get(f"{base}/builds/{int(build.get('id'))}/logs",
                                raw=True)
            except Exception:  # noqa: BLE001
                return ""
            text = log if isinstance(log, str) else ""
            return text[-limit:] if limit and len(text) > limit else text
        return ""

    def recent_change_requests(self, repo: str, state: str = "closed",
                               limit: int = 50) -> list[dict]:
        path = self._git(repo, "/pullrequests")
        if not path:
            return []
        wanted = {"closed": "completed", "open": "active",
                  "merged": "completed", "all": "all"}.get(
                      str(state or "closed").lower(), "completed")
        try:
            raw = self._get(path, {"searchCriteria.status": wanted,
                                   "$top": max(1, int(limit or 1))})
        except Exception:  # noqa: BLE001
            return []
        return [self._change_request(item, repo)
                for item in ((raw or {}).get("value") or [])][:limit]

    def open_issues(self, repo: str, limit: int = 100) -> list[dict]:
        """Every open work item in the project this repository belongs to.

        The project is the unit here, not the repository, so this answers for
        the project and keeps only what belongs to this repository — the same
        rule `assigned_open_issues` uses.
        """
        project, _ = self._split(repo)
        if not project:
            return []
        wiql = ("SELECT [System.Id] FROM WorkItems "
                "WHERE [System.State] NOT IN ('Closed','Done','Removed','Resolved') "
                "ORDER BY [System.ChangedDate] DESC")
        try:
            result = self._transport(
                "POST",
                f"{self.url}/{urllib.parse.quote(project)}/_apis/wit/wiql",
                headers=self._headers(),
                params={"api-version": API_VERSION,
                        "$top": max(1, int(limit or 1))},
                json_body={"query": wiql})
        except Exception:  # noqa: BLE001
            return []
        ids = [str(w.get("id")) for w in ((result or {}).get("workItems") or [])
               if w.get("id") is not None]
        out = []
        for raw in self._work_items(project, ids):
            if self._repo_for_work_item(raw, project) == repo:
                out.append(self._issue(raw, repo))
        return out[:limit]

    def create_issue(self, repo: str, title: str, body: str = "",
                     labels=None, assignees=None) -> int:
        """Open a work item. Returns its id, or 0.

        Created as a Task: it is the one type present in every stock process
        template. The `$` in the route is the API's, not a typo.
        """
        project, _ = self._split(repo)
        if not project:
            return 0
        ops = [{"op": "add", "path": "/fields/System.Title",
                "value": title or ""}]
        if body:
            ops.append({"op": "add", "path": "/fields/System.Description",
                        "value": body})
        names = [str(l).strip() for l in (labels or []) if str(l).strip()]
        if names:
            ops.append({"op": "add", "path": "/fields/System.Tags",
                        "value": "; ".join(names)})
        who = [str(a).strip() for a in (assignees or []) if str(a).strip()]
        if who:
            # One assignee only on this host; the rest would be silently
            # dropped by the API, so only the first is sent.
            ops.append({"op": "add", "path": "/fields/System.AssignedTo",
                        "value": who[0]})
        try:
            raw = self._transport(
                "POST",
                f"{self.url}/{urllib.parse.quote(project)}/_apis/wit/"
                f"workitems/$Task",
                headers=self._headers("application/json-patch+json"),
                params={"api-version": API_VERSION}, json_body=ops)
        except Exception:  # noqa: BLE001
            return 0
        try:
            return int((raw or {}).get("id") or 0)
        except (TypeError, ValueError):
            return 0

    def branch_head(self, repo: str, branch: str) -> str:
        path = self._git(repo, "/refs")
        if not path:
            return ""
        try:
            raw = self._get(path, {"filter": f"heads/{branch}",
                                   "$top": 1})
        except Exception:  # noqa: BLE001
            return ""
        for ref in ((raw or {}).get("value") or []):
            if _short_ref(ref.get("name")) == str(branch):
                return str(ref.get("objectId") or "")
        return ""

    def file_at_ref(self, repo: str, path: str, ref: str) -> str:
        """One file's text at one commit, or "" when it is not there."""
        base = self._git(repo, "/items")
        if not base:
            return ""
        wanted = str(path or "")
        try:
            got = self._get(base, {
                "scopePath": wanted if wanted.startswith("/") else f"/{wanted}",
                "versionDescriptor.version": str(ref or ""),
                "versionDescriptor.versionType": "commit",
                "includeContent": "true",
                "$format": "text",
            }, raw=True)
        except Exception:  # noqa: BLE001
            return ""
        return got if isinstance(got, str) else ""

    def comment_on_commit(self, repo: str, sha: str, body: str) -> bool:
        raise NotImplementedError(
            "Azure DevOps has no commit-comment API: a note attaches to a "
            "pull request thread or a work item, never to a commit on its "
            "own.")


def _short_ref(name) -> str:
    """`refs/heads/topic` -> `topic`. Refs are fully qualified on this host."""
    text = str(name or "")
    for prefix in ("refs/heads/", "refs/tags/"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text

"""GitLab, behind the forge interface.

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
    GITLAB,
)



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

    def _is_user(self, name: str) -> bool:
        """Does this name belong to ONE human account on this instance?

        A group answers no. So does a name that cannot be read at all, which
        is deliberate: an unreadable name is not proof that it is safe to
        notify, and the whole point of this check is that the expensive
        mistake is in the other direction.
        """
        cached = self._mention_seen(name)
        if cached is not None:
            return cached
        try:
            found = self._get("/users", {"username": name})
        except Exception:  # noqa: BLE001 - unreadable is "not a person"
            found = None
        ok = bool(isinstance(found, list) and len(found) == 1
                  and isinstance(found[0], dict)
                  and str(found[0].get("username") or "").lower() == name.lower())
        return self._mention_seen(name, ok)

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

    # The access level GitLab gives an Owner. Maintainer is 40, and a
    # Maintainer is not who a project belongs to.
    _OWNER_ACCESS = 50

    def owner_login(self, repo: str) -> str:
        """The project's CREATOR, falling back to one owner-level member.

        A GitLab project path starts with a GROUP, and a group can hold dozens
        of Owners — inheriting them from every group above it. Asking "who
        owns this?" and taking the answer whole is how one issue reached
        forty-two people. The project record names the ONE account that
        created it, and that is the human this bot talks to.

        The fallback runs only when there is no readable creator (the account
        was deleted, blocked, or is the bot itself). It reads the members with
        Owner access — `/members/all` so inherited ones count — and takes the
        longest-standing of them, which is the lowest user id. One name, and
        the same name on every tick.
        """
        bot = (self.bot_identity() or "").lower()
        try:
            project = self._get(f"/projects/{self._project(repo)}")
        except Exception:  # noqa: BLE001 - unreadable is "cannot name one"
            project = None
        if not isinstance(project, dict):
            project = {}
        creator_id = project.get("creator_id")
        if creator_id:
            name = self._active_username(creator_id)
            if name and name.lower() != bot:
                return name
        try:
            members = self._get(f"/projects/{self._project(repo)}/members/all",
                                {"per_page": "100"})
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(members, list):
            return ""
        owners = [m for m in members
                  if isinstance(m, dict)
                  and int(m.get("access_level") or 0) >= self._OWNER_ACCESS
                  and str(m.get("state") or "active") == "active"
                  and str(m.get("username") or "").lower() != bot
                  and str(m.get("username") or "").strip()]
        if not owners:
            return ""
        owners.sort(key=lambda m: int(m.get("id") or 0))
        return str(owners[0].get("username") or "")

    def _active_username(self, user_id) -> str:
        """One account's name, or "" when it is gone or blocked.

        A creator who has left the instance still has an id on the project.
        Their name would @-mention nobody and assign to an account that cannot
        answer, so a non-active account is the same as no answer at all.
        """
        try:
            user = self._get(f"/users/{user_id}")
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(user, dict):
            return ""
        if str(user.get("state") or "") != "active":
            return ""
        return str(user.get("username") or "")

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
            {"body": self._one_human_only(body)})

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
            {"body": self._one_human_only(body)})

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
        return self._write("POST", f"{path}/notes",
                           {"body": self._one_human_only(body)})

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
        # One reviewer. "Ask the owners to review" is the same mistake as
        # assigning them: a review that belongs to everybody belongs to no one,
        # and every one of them gets the notification.
        return self._write(
            "PUT", f"/projects/{self._project(repo)}/merge_requests/{number}",
            {"reviewer_ids": str(ids[0])})

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
        fields = {"title": title,
                  "description": self._one_human_only(body or "")}
        names = [str(l) for l in (labels or []) if str(l).strip()]
        if names:
            fields["labels"] = ",".join(names)
        # Assignees are numeric ids here, so each account name is resolved.
        # A name that does not resolve is dropped rather than guessed at: an
        # id for the wrong account assigns somebody else's work to a stranger.
        #
        # ONE id reaches the host, whatever the caller passed. A group path
        # resolves to nothing here and is dropped already; the cap is for the
        # other half of the same mistake — a caller handing over a LIST of
        # owners, which assigns the work to a crowd and belongs to nobody.
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
            fields["assignee_ids"] = str(ids[0])
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

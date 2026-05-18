#!/usr/bin/env python3
"""
heartbeat-issue-tick: emit a JSON spawn plan for the issue-watcher CronJob.

Lists every repo the bot is a collaborator on, then for each repo lists
open issues assigned to @me and the in-flight K8s Jobs that are already
fixing them. The K8s control plane is the concurrency ledger — Jobs
labeled `app=issue-fixer, issueRepo=<slug>` count toward the per-repo
cap (default 2). Jobs that have completed (succeeded/failed) or aged
past TTL no longer count.

The script is **read-only**. It just decides which (repo, issue) pairs
the spawner script may launch a Job for. The spawner enforces the same
limit again at create-time as a belt-and-suspenders check.

Env:
  GITHUB_TOKEN              — bot's PAT (already wired)
  HEARTBEAT_MAX_PER_REPO    (default 2)
  HEARTBEAT_TTL_SECONDS     (default 3600 — both K8s activeDeadline
                             and the upper bound on "we still count
                             this Job as alive even without status")
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_PER_REPO = int(os.environ.get("HEARTBEAT_MAX_PER_REPO", "2"))
TTL_SECONDS = int(os.environ.get("HEARTBEAT_TTL_SECONDS", "3600"))

K8S_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_API = "https://kubernetes.default.svc"
HTTP_TIMEOUT = 15


def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def gh_get(url: str, params: dict | None = None) -> list | dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "openclaw-issue-watcher",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def list_all_assigned_open_issues() -> dict[str, list[dict]]:
    """Return {repo_full_name: [issue, ...]} for all open issues assigned
    to the authenticated user across every repo they can see.

    Uses the cross-repo `GET /issues?filter=assigned` endpoint rather than
    per-repo `/repos/{owner}/{repo}/issues?assignee=...` — that param
    returns 422 unless given a literal login (no `@me` shortcut), and
    one call here replaces N round-trips.
    """
    by_repo: dict[str, list[dict]] = {}
    page = 1
    while True:
        batch = gh_get(
            "https://api.github.com/issues",
            {"filter": "assigned", "state": "open", "per_page": 100, "page": page},
        )
        for i in batch:
            if "pull_request" in i:
                continue  # /issues returns PRs too; strip them.
            # repository_url like https://api.github.com/repos/owner/name
            repo = i["repository_url"].rsplit("/repos/", 1)[1]
            by_repo.setdefault(repo, []).append(i)
        if len(batch) < 100:
            break
        page += 1
    return by_repo


def repo_slug(repo: str) -> str:
    # K8s label values: [a-z0-9.-_], so map "owner/name" to "owner.name".
    return repo.replace("/", ".").lower()


def k8s_get_jobs(namespace: str, label_selector: str) -> list[dict]:
    # In-cluster auth: SA token + CA bundle live under /var/run/secrets.
    # No third-party deps — just urllib over TLS with the kubelet's CA.
    token = _read(f"{K8S_SA_DIR}/token")
    ctx = ssl.create_default_context(cafile=f"{K8S_SA_DIR}/ca.crt")
    url = (
        f"{K8S_API}/apis/batch/v1/namespaces/{namespace}/jobs"
        f"?labelSelector={urllib.parse.quote(label_selector)}"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
        body = json.loads(r.read())
    return body.get("items", [])


def is_active(job: dict, now_epoch: float) -> bool:
    """A Job counts as alive if K8s hasn't recorded completion AND it's
    still inside the TTL window. Cleaning up stuck Jobs is K8s's job via
    activeDeadlineSeconds; we double-check here so a CronJob tick can't
    keep slots permanently held by a controller-side hiccup."""
    status = job.get("status") or {}
    if status.get("completionTime"):
        return False
    if any(c.get("type") == "Failed" and c.get("status") == "True"
           for c in status.get("conditions") or []):
        return False
    start = status.get("startTime") or job["metadata"].get("creationTimestamp")
    if start:
        # ISO 8601 from K8s, e.g. "2026-05-18T08:00:00Z"
        import datetime
        ts = datetime.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        ).timestamp()
        if now_epoch - ts > TTL_SECONDS:
            return False
    return True


def main() -> int:
    if not GITHUB_TOKEN:
        json.dump({"error": "GITHUB_TOKEN not set"}, sys.stdout)
        return 1
    try:
        namespace = _read(f"{K8S_SA_DIR}/namespace")
    except FileNotFoundError:
        json.dump({"error": "not running in a pod (no service-account dir)"}, sys.stdout)
        return 1

    started = time.time()
    try:
        issues_by_repo = list_all_assigned_open_issues()
    except urllib.error.HTTPError as e:
        json.dump({"error": f"list assigned issues: {e.code} {e.reason}"}, sys.stdout)
        return 2

    plan: dict = {
        "generatedAt": int(started),
        "namespace": namespace,
        "ttlSeconds": TTL_SECONDS,
        "maxPerRepo": MAX_PER_REPO,
        "repos": [],
    }

    # Only consider repos where the bot actually has assigned issues —
    # if /issues?filter=assigned returned nothing for a repo, there is
    # nothing for the watcher to do there even if it has collaborator
    # access. Skips iterating over collab repos with empty queues.
    for repo, issues in sorted(issues_by_repo.items()):
        slug = repo_slug(repo)
        try:
            active_jobs = k8s_get_jobs(
                namespace, f"app=issue-fixer,issueRepo={slug}"
            )
        except urllib.error.HTTPError as e:
            plan["repos"].append(
                {"repo": repo, "error": f"k8s list jobs: {e.code} {e.reason}"}
            )
            continue

        live = [j for j in active_jobs if is_active(j, started)]
        live_issue_numbers = {
            int(j["metadata"]["labels"].get("issueNumber", "0"))
            for j in live
            if j["metadata"].get("labels", {}).get("issueNumber")
        }
        slots = max(0, MAX_PER_REPO - len(live))

        candidates = [i for i in issues if i["number"] not in live_issue_numbers]
        to_spawn = [
            {
                "issueNumber": i["number"],
                "title": i["title"],
                "url": i["html_url"],
                "labels": [l["name"] for l in i.get("labels", [])],
            }
            for i in candidates[:slots]
        ]

        plan["repos"].append(
            {
                "repo": repo,
                "slug": slug,
                "activeJobs": len(live),
                "activeIssueNumbers": sorted(live_issue_numbers),
                "openAssignedCount": len(issues),
                "candidateCount": len(candidates),
                "toSpawn": to_spawn,
                "deferredDueToLimit": max(0, len(candidates) - slots),
            }
        )

    plan["elapsedSeconds"] = round(time.time() - started, 2)
    json.dump(plan, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

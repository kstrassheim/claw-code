#!/usr/bin/env python3
"""
heartbeat-issue-tick: emit a JSON spawn plan for the issue-watcher CronJob.

Lists every open GitHub issue assigned to the bot (one cross-repo call),
then queries the openclaw container's filesystem to see which repos
already have an in-flight fixer (lockdir under
~/.openclaw/projects/<repo>/.fixer.lock). At most ONE fixer per repo
runs at a time — they share the on-disk checkout, so two subprocesses
on the same repo would race.

Lock TTL: if a fixer dies without its bash `trap` firing, the lock
stays. The planner treats locks older than HEARTBEAT_TTL_SECONDS as
stale and ignores them (the next fixer will reuse the dir, and the
`mkdir` race resolves cleanly).

The script is read-only against both GitHub and the openclaw pod.

Env:
  GITHUB_TOKEN              — bot's PAT (already wired)
  HEARTBEAT_MAX_PER_REPO    (default 1)
  HEARTBEAT_TTL_SECONDS     (default 3600)
"""

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_PER_REPO = int(os.environ.get("HEARTBEAT_MAX_PER_REPO", "1"))
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
    by_repo: dict[str, list[dict]] = {}
    page = 1
    while True:
        batch = gh_get(
            "https://api.github.com/issues",
            {"filter": "assigned", "state": "open", "per_page": 100, "page": page},
        )
        for i in batch:
            if "pull_request" in i:
                continue
            repo = i["repository_url"].rsplit("/repos/", 1)[1]
            by_repo.setdefault(repo, []).append(i)
        if len(batch) < 100:
            break
        page += 1
    return by_repo


def k8s_find_openclaw_pod(namespace: str) -> str:
    token = _read(f"{K8S_SA_DIR}/token")
    ctx = ssl.create_default_context(cafile=f"{K8S_SA_DIR}/ca.crt")
    url = f"{K8S_API}/api/v1/namespaces/{namespace}/pods?labelSelector=app%3Dclaw-code"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
        body = json.loads(r.read())
    for item in body.get("items", []):
        if item.get("status", {}).get("phase") == "Running":
            return item["metadata"]["name"]
    raise RuntimeError("no Running openclaw pod found")


def kubectl_exec_capture(namespace: str, pod: str, *cmd: str, timeout: int = 15) -> tuple[int, str, str]:
    """Run a command inside the openclaw pod, capture stdout/stderr."""
    full = ["kubectl", "-n", namespace, "exec", pod, "-c", "openclaw", "--", *cmd]
    proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def list_locked_repos(namespace: str, pod: str) -> set[str]:
    """Return set of repo full_names whose lock dir under
    ~/.openclaw/.fixer-locks/<owner>__<name>/ exists and is newer than
    TTL_SECONDS. Each lock corresponds to one in-flight fixer (per-repo
    cap is 1, because the on-disk checkout can't be shared)."""
    # `find` is faster than a recursive ls; the lock-set is small
    # (≤ one dir per repo cameron-claw is a collaborator on). Lock
    # dirs are siblings of the project tree (NOT inside it — a
    # `.fixer.lock` inside the project dir broke `git clone`).
    script = (
        "set -eu; root=$HOME/.openclaw/.fixer-locks; "
        "[ -d $root ] || exit 0; "
        f"now=$(date +%s); ttl={TTL_SECONDS}; "
        "for lock in $(find $root -maxdepth 1 -mindepth 1 -type d 2>/dev/null); do "
        "  age=$(( now - $(stat -c %Y $lock) )); "
        "  if [ $age -lt $ttl ]; then "
        # Lock dir name is owner__name → emit owner/name
        "    basename $lock | sed 's|__|/|'; "
        "  fi; "
        "done"
    )
    rc, out, err = kubectl_exec_capture(namespace, pod, "bash", "-c", script)
    if rc != 0:
        sys.stderr.write(f"list_locked_repos: exec rc={rc} stderr={err}\n")
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


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

    try:
        openclaw_pod = k8s_find_openclaw_pod(namespace)
    except Exception as e:
        json.dump({"error": f"find openclaw pod: {e}"}, sys.stdout)
        return 3

    locked = list_locked_repos(namespace, openclaw_pod)

    plan: dict = {
        "generatedAt": int(started),
        "namespace": namespace,
        "openclawPod": openclaw_pod,
        "ttlSeconds": TTL_SECONDS,
        "maxPerRepo": MAX_PER_REPO,
        "repos": [],
    }

    for repo, issues in sorted(issues_by_repo.items()):
        is_locked = repo in locked
        # MAX_PER_REPO is 1 by design — checkouts can't be shared.
        # If the lock is held, we skip all issues for this repo until
        # the next tick.
        if is_locked:
            to_spawn = []
            deferred = len(issues)
        else:
            to_spawn = [
                {
                    "issueNumber": i["number"],
                    "title": i["title"],
                    "url": i["html_url"],
                    "labels": [l["name"] for l in i.get("labels", [])],
                }
                for i in issues[:MAX_PER_REPO]
            ]
            deferred = max(0, len(issues) - MAX_PER_REPO)

        plan["repos"].append(
            {
                "repo": repo,
                "locked": is_locked,
                "openAssignedCount": len(issues),
                "toSpawn": to_spawn,
                "deferredDueToLimit": deferred,
            }
        )

    plan["elapsedSeconds"] = round(time.time() - started, 2)
    json.dump(plan, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

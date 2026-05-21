#!/usr/bin/env python3
"""
tester-tick: emit a JSON spawn plan for the tester CronJob.

Lists every repo the bot is a collaborator on (or owner of) that has
at least one assignable workflow / branch (i.e., a real project), then
queries the openclaw container's filesystem to skip repos that:
  - already have a tester-runner in flight (lock dir present)
  - had their current main HEAD already tested (last-head file matches)

The script is read-only against both GitHub and the openclaw pod.

Output (stdout):
  {
    "namespace": "...",
    "repos": [ {"repo": "owner/name", "headSha": "...", "toSpawn": true/false, "reason": "..."}, ... ]
  }

cron-tester-spawn.sh consumes the plan and kubectl-exec's a tester-
runner into the openclaw pod for each repo with toSpawn=true.

Env:
  GITHUB_TOKEN              bot's PAT (already wired)
  TESTER_TTL_SECONDS        stale-lock cutoff, default 7200 (2h — tester
                            runs are slower than fixer, give them more
                            time before considering a lock stale)
  TESTER_MAX_REPOS          safety cap on repos per tick (default 8)
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
TTL_SECONDS = int(os.environ.get("TESTER_TTL_SECONDS", "7200"))
MAX_REPOS = int(os.environ.get("TESTER_MAX_REPOS", "8"))

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
            "User-Agent": "tester-tick/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"GitHub {e.code} on {url}: {body[:200]}") from None


def list_candidate_repos() -> list[str]:
    """Return repos the bot user owns OR collaborates on. We don't
    filter by topic/visibility — that's the user's responsibility
    via the repo collaborator graph. Cap at MAX_REPOS so a bot that
    is in many repos doesn't fan out unbounded per tick."""
    # /user/repos returns all repos the authed user can access:
    # owned, collaborator, org-member. affiliation=owner,collaborator
    # excludes org-membership noise. `type` is mutually exclusive with
    # `affiliation` (GitHub returns 422), so we only send affiliation.
    repos = gh_get(
        "https://api.github.com/user/repos",
        {
            "affiliation": "owner,collaborator",
            "sort": "pushed",
            "direction": "desc",
            "per_page": str(MAX_REPOS),
        },
    )
    return [r["full_name"] for r in repos][:MAX_REPOS]


def head_sha_for_default_branch(full_name: str) -> tuple[str, str]:
    """Return (default_branch, head_sha). Empty strings if anything
    goes wrong — caller treats that as "skip this repo"."""
    try:
        repo = gh_get(f"https://api.github.com/repos/{full_name}")
    except Exception:
        return ("", "")
    default_branch = repo.get("default_branch") or "main"
    try:
        branch = gh_get(
            f"https://api.github.com/repos/{full_name}/branches/{default_branch}"
        )
    except Exception:
        return (default_branch, "")
    sha = ((branch.get("commit") or {}).get("sha")) or ""
    return (default_branch, sha)


# ---- pod-side queries -------------------------------------------------


def k8s_token() -> str:
    return _read(f"{K8S_SA_DIR}/token")


def k8s_namespace() -> str:
    return _read(f"{K8S_SA_DIR}/namespace")


def find_openclaw_pod(namespace: str) -> str:
    """Reuse the same labelSelector logic as the fixer planner. Some
    deployments have only `app=openclaw`; others add `component=server`."""
    for selector in ("app=openclaw,component=server", "app=openclaw"):
        try:
            ctx = ssl.create_default_context(cafile=f"{K8S_SA_DIR}/ca.crt")
            url = (
                f"{K8S_API}/api/v1/namespaces/{namespace}/pods"
                f"?labelSelector={urllib.parse.quote(selector)}"
            )
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {k8s_token()}",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
                data = json.loads(r.read())
            for p in data.get("items", []):
                if (p.get("status") or {}).get("phase") == "Running":
                    return p["metadata"]["name"]
        except Exception:
            continue
    return ""


def query_pod_state(namespace: str, pod: str) -> tuple[set[str], dict[str, str]]:
    """Return (set of locked-repo keys, dict mapping repo_key →
    last-head SHA). repo_key is `owner__name` to match the runner's
    encoding."""
    if not pod:
        return (set(), {})

    script = (
        "set -eu; "
        # locks
        "root=$HOME/.openclaw/.tester-locks; "
        f"now=$(date +%s); ttl={TTL_SECONDS}; "
        'echo "==LOCKS=="; '
        "if [ -d $root ]; then "
        "  for d in $(find $root -maxdepth 1 -mindepth 1 -type d 2>/dev/null); do "
        "    age=$(( now - $(stat -c %Y \"$d\") )); "
        "    if [ $age -lt $ttl ]; then echo $(basename \"$d\"); fi; "
        "  done; "
        "fi; "
        # last-head files
        'echo "==HEADS=="; '
        "state=$HOME/.openclaw/tester-state; "
        "if [ -d $state ]; then "
        "  for f in $(find $state -maxdepth 1 -mindepth 1 -name '*.last-head' 2>/dev/null); do "
        '    base=$(basename "$f" .last-head); '
        '    head=$(cat "$f" 2>/dev/null); '
        "    echo \"$base $head\"; "
        "  done; "
        "fi"
    )
    try:
        proc = subprocess.run(
            ["kubectl", "-n", namespace, "exec", pod, "-c", "openclaw", "--",
             "bash", "-c", script],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            return (set(), {})
    except Exception:
        return (set(), {})

    locks: set[str] = set()
    heads: dict[str, str] = {}
    section = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line == "==LOCKS==":
            section = "locks"
            continue
        if line == "==HEADS==":
            section = "heads"
            continue
        if not line:
            continue
        if section == "locks":
            locks.add(line)
        elif section == "heads":
            parts = line.split()
            if len(parts) >= 2:
                heads[parts[0]] = parts[1]
    return (locks, heads)


# ---- main -----------------------------------------------------------


def main() -> None:
    namespace = k8s_namespace()
    pod = find_openclaw_pod(namespace)

    locks, last_heads = query_pod_state(namespace, pod)

    out_repos: list[dict] = []
    try:
        candidates = list_candidate_repos()
    except Exception as e:
        print(json.dumps({"namespace": namespace, "error": str(e), "repos": []}))
        return

    for full_name in candidates:
        repo_key = full_name.replace("/", "__")
        if repo_key in locks:
            out_repos.append({
                "repo": full_name,
                "toSpawn": False,
                "reason": "lock-held",
            })
            continue
        default_branch, sha = head_sha_for_default_branch(full_name)
        if not sha:
            out_repos.append({
                "repo": full_name,
                "toSpawn": False,
                "reason": "head-fetch-failed",
            })
            continue
        prior = last_heads.get(repo_key, "")
        if sha == prior:
            out_repos.append({
                "repo": full_name,
                "toSpawn": False,
                "reason": "head-unchanged",
                "headSha": sha,
            })
            continue
        out_repos.append({
            "repo": full_name,
            "toSpawn": True,
            "headSha": sha,
            "defaultBranch": default_branch,
            "priorHead": prior,
        })

    print(json.dumps({"namespace": namespace, "repos": out_repos}))


if __name__ == "__main__":
    main()

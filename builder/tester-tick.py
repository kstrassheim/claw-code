#!/usr/bin/env python3
"""
tester-tick: emit a JSON spawn plan for the tester CronJob.

The candidate list is the ALLOWED-PROJECTS LIST — the repositories the owner
permitted with `projects add` — and not a "recently pushed" query against the
code host. That is a correctness fix, not a shortcut: a query sorted by push
date returns the most recently active repositories, so a permitted repository
that is simply quiet sits below the cut and is never tested at all, silently
and forever. The permitted list names exactly what may be tested, so there is
nothing to discover and no reason to spend a request discovering it.

Discovery survives as the path taken when the list cannot be read — there it
produces candidates that main() then denies one by one, so the tick reports
WHY it did nothing instead of looking idle.

For each candidate the planner then skips repos that:
  - are not permitted (`project-allow check` inside the pod; exit 2 = no)
  - already have a tester-runner in flight (live lock dir present)
  - had their current default-branch HEAD already tested (last-head matches)

And before any of that, the whole tick is held back while the issue solver or
the pull-request reviewer still have work queued — "first solve and merge,
then test". See queue_state.py, including why a stale marker must FAIL OPEN.

EVERY QUESTION GOES THROUGH forge.py. This file contains no request, no
endpoint and no host-specific field name.

The script is read-only against both the code host and the openclaw pod.

Output (stdout):
  {
    "namespace": "...",
    "repos": [ {"repo": "owner/name", "headSha": "...", "toSpawn": true/false, "reason": "..."}, ... ]
  }

or, when the tick is held back:

  {"namespace": "...", "repos": [], "skipped": "<why>", "queues": {...}}

cron-tester-spawn.sh consumes the plan and kubectl-exec's a tester-
runner into the openclaw pod for each repo with toSpawn=true.

Env:
  GITHUB_TOKEN              bot's credentials on GitHub
  GITLAB_URL / GITLAB_API_TOKEN
  GITEA_URL / GITEA_API_TOKEN
                            bot's credentials on GitLab; unset means the host
                            is skipped, which is the normal state
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
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forge  # noqa: E402
import project_allowlist  # noqa: E402
import queue_state  # noqa: E402
from project_allowlist import Allowlist  # noqa: E402

# The code hosts this deployment has credentials for, and nothing else. Built
# once so the whole tick asks the same objects; replaced by the tests, which
# drive a fake in its place and so make no request at all.
FORGES = forge.configured()

TTL_SECONDS = int(os.environ.get("TESTER_TTL_SECONDS", "7200"))
MAX_REPOS = int(os.environ.get("TESTER_MAX_REPOS", "8"))

K8S_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_API = "https://kubernetes.default.svc"
HTTP_TIMEOUT = 15


def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def discover_repos() -> list[str]:
    """Repos the bot owns OR collaborates on, most recently pushed first.

    The FALLBACK candidate source, used only when the allowed-projects list
    could not be read. It is not the normal path precisely because of the
    ordering: activity-sorted plus a cap means a quiet repository can never
    appear, and a tester that silently stops covering a repository is
    indistinguishable from one that finds nothing wrong with it.
    """
    return FORGES.accessible_repos(MAX_REPOS)


def candidate_repos(allowed: Allowlist) -> list[str]:
    """The repositories this tick will consider, permitted ones first.

    The permitted list IS the candidate list. Only when it is unreadable do we
    fall back to discovery — and those candidates are then denied one by one
    below, which is the point: a tick that tested nothing says whether nothing
    was permitted or the list could not be read.
    """
    if allowed.available and allowed.entries:
        if len(allowed.entries) > MAX_REPOS:
            # Say it out loud. A silent cap here reads as "these repositories
            # are being tested" when the ones past the cut never are — raise
            # TESTER_MAX_REPOS rather than wonder why a repository is quiet.
            sys.stderr.write(
                f"WARNING: {len(allowed.entries)} repositories permitted but "
                f"TESTER_MAX_REPOS={MAX_REPOS}; not considered this tick: "
                + ", ".join(allowed.entries[MAX_REPOS:]) + "\n"
            )
        return allowed.entries[:MAX_REPOS]
    if allowed.available:
        # Read the list, and it permits nothing. Discovery here would hand back
        # repositories the owner deliberately did not grant.
        return []
    return discover_repos()


def head_sha_for_default_branch(full_name: str) -> tuple[str, str]:
    """Return (default_branch, head_sha). Empty strings if anything
    goes wrong — caller treats that as "skip this repo".

    No configured host is one of the things that can go wrong, and it is
    reported the same way: the repository is skipped with a reason rather than
    the tick dying, so a deployment missing its credentials still says what it
    could not do for every repository it was asked about.
    """
    if not FORGES:
        return ("", "")
    return FORGES.of(full_name).default_branch_head(full_name)


# ---- pod-side queries -------------------------------------------------


def k8s_token() -> str:
    return _read(f"{K8S_SA_DIR}/token")


def k8s_namespace() -> str:
    return _read(f"{K8S_SA_DIR}/namespace")


def find_openclaw_pod(namespace: str) -> str:
    """Reuse the same labelSelector logic as the fixer planner. Some
    deployments have only `app=<label>`; others add `component=server`."""
    app_label = os.environ.get("OPENCLAW_APP_LABEL", "claw-code")
    for selector in (f"app={app_label},component=server", f"app={app_label}"):
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


def pod_exec(namespace: str, pod: str, script: str, timeout: int = 20):
    return subprocess.run(
        ["kubectl", "-n", namespace, "exec", pod, "-c",
         os.environ.get("OPENCLAW_CONTAINER", "claw-code"), "--",
         "bash", "-c", script],
        capture_output=True, text=True, timeout=timeout,
    )


def query_pod_state(
    namespace: str, pod: str
) -> tuple[set[str], dict[str, str], Allowlist, dict[str, tuple[int, int]]]:
    """Return (locked repo keys, {repo_key → last-head SHA}, the permitted
    list, the other subsystems' queue depths). repo_key is `owner__name`, to
    match the runner's encoding.

    The allowlist and the queue markers ride along in this one exec: they live
    on the same PVC as the locks and the state files, so reading them costs
    nothing extra. If the exec fails we cannot tell what is permitted, and an
    unreadable permission list permits nothing (Allowlist.denied) — while an
    unreadable queue marker permits testing, which is the opposite direction
    and deliberately so (see queue_state.py).
    """
    if not pod:
        return (set(), {}, Allowlist.denied(), {})

    script = (
        "set -eu; "
        # locks
        "root=$HOME/.openclaw/.tester-locks; "
        f"now=$(date +%s); ttl={TTL_SECONDS}; "
        'echo "==LOCKS=="; '
        "if [ -d $root ]; then "
        "  for d in $(find $root -maxdepth 1 -mindepth 1 -type d 2>/dev/null); do "
        "    age=$(( now - $(stat -c %Y \"$d\") )); "
        "    [ $age -lt $ttl ] || continue; "
        # Age alone is not enough. A runner killed without its EXIT trap (pod
        # restart, SIGKILL) leaves the directory behind, and a planner that
        # trusted it would refuse to spawn for the whole TTL — so the runner's
        # own stale-lock handling never gets the chance to run either. The
        # owner PID must still be a live tester-runner.
        "    pid=$(awk '{print $1; exit}' \"$d/owner\" 2>/dev/null || true); "
        "    [ -n \"$pid\" ] || continue; "
        "    [ -d \"/proc/$pid\" ] || continue; "
        "    grep -aq tester-runner \"/proc/$pid/cmdline\" 2>/dev/null || continue; "
        "    echo $(basename \"$d\"); "
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
        "fi; "
        # allowed-projects list — same PVC, same exec
        + project_allowlist.pod_read_snippet()
        # what the other two subsystems still have queued — same PVC again
        + queue_state.pod_read_snippet()
    )
    try:
        proc = pod_exec(namespace, pod, script)
        if proc.returncode != 0:
            return (set(), {}, Allowlist.denied(), {})
    except Exception:
        return (set(), {}, Allowlist.denied(), {})

    locks: set[str] = set()
    heads: dict[str, str] = {}
    allowed_lines: list[str] = []
    queue_lines: list[str] = []
    section = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line == "==LOCKS==":
            section = "locks"
            continue
        if line == "==HEADS==":
            section = "heads"
            continue
        if line == project_allowlist.ALLOWED_MARKER:
            section = "allowed"
            continue
        if line == queue_state.MARKER:
            section = "queues"
            continue
        if not line:
            continue
        if section == "locks":
            locks.add(line)
        elif section == "heads":
            parts = line.split()
            if len(parts) >= 2:
                heads[parts[0]] = parts[1]
        elif section == "allowed":
            allowed_lines.append(line)
        elif section == "queues":
            queue_lines.append(line)
    return (
        locks,
        heads,
        project_allowlist.from_section(allowed_lines),
        queue_state.parse(queue_lines),
    )


def query_permissions(namespace: str, pod: str,
                      repos: list[str]) -> tuple[dict[str, int], bool]:
    """Ask `project-allow check` about each repo, inside the pod that holds the
    list on its PVC. Returns ({repo: exit code}, list-was-readable).

    The CLI is the authority rather than a second parse of the file here: one
    definition of "permitted", four readers. Exit 2 is "not permitted" — an
    answer. Anything else (the CLI missing, the list unreadable, the exec
    failing) means we could not find out, which permits NOTHING, and is
    reported so the spawner can say why nothing was tested rather than looking
    idle.
    """
    if not pod or not repos:
        return ({}, bool(pod))
    quoted = " ".join("'" + r.replace("'", "'\\''") + "'" for r in sorted(set(repos)))
    script = (
        f"for r in {quoted}; do "
        "  project-allow check \"$r\" >/dev/null 2>&1; "
        "  echo \"$? $r\"; "
        "done"
    )
    try:
        proc = pod_exec(namespace, pod, script)
        if proc.returncode != 0:
            return ({}, False)
    except Exception:
        return ({}, False)

    codes: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            codes[parts[1]] = int(parts[0])
        except ValueError:
            continue
    if not codes:
        return ({}, False)
    return (codes, True)


def permission_reason(code: int | None) -> str:
    """Why a repo was refused, or "" when it was not."""
    if code == 0:
        return ""
    if code == 2:
        return "not-permitted"
    return "allowlist-unavailable"


# ---- main -----------------------------------------------------------


def main() -> None:
    namespace = k8s_namespace()
    pod = find_openclaw_pod(namespace)

    locks, last_heads, allowed, queues = query_pod_state(namespace, pod)

    # "First solve and merge, then test." Testing is the most expensive
    # subsystem and the least urgent, so it waits until the issue solver and
    # the reviewer have emptied their queues — across ALL permitted
    # repositories, not just the one being considered.
    #
    # Nothing is recorded on this path: no last-head is written, so a commit
    # skipped here is picked up again on a later tick rather than being marked
    # as tested. A stale marker does NOT block — see queue_state.py on why "no
    # news" must never become a permanent silent shutdown of testing.
    blocked = queue_state.blocking_reason(queues)
    if blocked:
        print(json.dumps({
            "namespace": namespace,
            "repos": [],
            "skipped": blocked,
            "queues": {k: v[0] for k, v in sorted(queues.items())},
        }))
        return

    try:
        candidates = candidate_repos(allowed)
    except Exception as e:  # noqa: BLE001
        # Only reachable with an UNREADABLE permitted list (the sole path that
        # calls discovery). Deliberately not reported as a planner `error`,
        # which makes the spawner exit 1 with a generic message: the plan
        # below carries `allowlistAvailable: false`, which is the specific,
        # actionable thing to say — the list is what needs fixing, not the
        # GitHub query that was only ever a fallback.
        sys.stderr.write(f"candidate discovery failed: {type(e).__name__}: {e}\n")
        candidates = []

    # Permission first — one exec for every candidate, ahead of the per-repo
    # API calls it would otherwise pay for. Nothing about a repository the bot
    # may not touch is worth spending a request on.
    perms, allowlist_available = query_permissions(namespace, pod, candidates)

    out_repos: list[dict] = []
    for full_name in candidates:
        repo_key = full_name.replace("/", "__")
        denied = permission_reason(perms.get(full_name))
        if denied:
            out_repos.append({
                "repo": full_name,
                "toSpawn": False,
                "reason": denied,
            })
            continue
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

    print(json.dumps({
        "namespace": namespace,
        "repos": out_repos,
        # Surfaced so a tick that tested nothing says why: "0 permitted" and
        # "could not read the list" look identical from the repos array alone.
        "allowedProjects": len(allowed) if allowed.available else None,
        "allowlistAvailable": bool(allowed.available and allowlist_available),
        "queues": {k: v[0] for k, v in sorted(queues.items())},
    }))


if __name__ == "__main__":
    main()

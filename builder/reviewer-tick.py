#!/usr/bin/env python3
"""
reviewer-tick: emit a JSON spawn plan for the pr-reviewer CronJob.

Finds every OPEN change request the bot should look at — on every code host
this deployment has credentials for — then queries the openclaw container's
filesystem to skip the ones that:
  - belong to a repo with a reviewer-runner already in flight (lock dir under
    ~/.openclaw/.reviewer-locks/<owner>__<name>/ — one reviewer per repo,
    because the review checkout can't be shared)
  - already had their current head reviewed, with the same title and body
    (see review_subject.py — a verdict about the DESCRIPTION has to be
    clearable by editing the description, not only by pushing a commit)
and gates the rest on the head commit's checks being green — the reviewer only
reviews green pull requests. The issue-solver likewise waits for green before
requesting a review; for human-authored pull requests this makes the reviewer
wait for CI instead of reviewing code that does not build.

HOW THE CHANGE REQUESTS ARE FOUND
---------------------------------
`forge.reviewable_change_requests` answers that, and each host answers it its
own way — see forge.py for why authorship rather than a review request is the
primary signal on one of them, and why a listing result carries no head
commit anywhere.

That last part is what the gate costs: the listing has no check state and no
head commit, so each candidate is fetched (`change_request`) and its head
commit's CI reduced (`checks_state`). Capped by REVIEWER_MAX_PRS.

Pull requests in repos that are not on the bot's allowed-projects list are
dropped before any of that — see project_allowlist.py and `project-allow`.
Being asked for a review is how someone REQUESTS one; the list is the answer.

EVERY QUESTION GOES THROUGH forge.py. This file contains no request, no
endpoint and no host-specific field name.

The script is read-only against both the code host and the openclaw pod.

Output (stdout):
  {
    "namespace": "...",
    "prs": [ {"repo": "owner/name", "prNumber": N, "toSpawn": true/false,
              "reason": "...", "headSha": "...", "headRef": "...",
              "baseRef": "..."} ]
  }

cron-reviewer-spawn.sh consumes the plan and kubectl-exec's a reviewer-runner
into the openclaw pod for each entry with toSpawn=true.

Env:
  GITHUB_TOKEN              bot's credentials on GitHub
  GITLAB_URL / GITLAB_API_TOKEN
                            bot's credentials on GitLab; unset means the host
                            is skipped, which is the normal state
  REVIEWER_TTL_SECONDS      stale-lock cutoff, default 7200 (reviews run the
                            app locally — give them tester-like time)
  REVIEWER_MAX_PRS          safety cap on pull requests per tick (default 8)
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
import issue_priority  # noqa: E402
import review_subject  # noqa: E402
from forge import FAILED, GREEN, NONE, PENDING  # noqa: E402

# The code hosts this deployment has credentials for, and nothing else. Built
# once so the whole tick asks the same objects; replaced by the tests, which
# drive a fake in its place and so make no request at all.
FORGES = forge.configured()

TTL_SECONDS = int(os.environ.get("REVIEWER_TTL_SECONDS", "7200"))
MAX_PRS = int(os.environ.get("REVIEWER_MAX_PRS", "8"))

K8S_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_API = "https://kubernetes.default.svc"
HTTP_TIMEOUT = 15


def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def bot_login() -> str:
    """Who this tick is reviewing as, across every configured host.

    One name per host in principle; in practice a deployment authenticates as
    one account and the plan reports it for the operator's benefit. The first
    host that answers names the tick.
    """
    for f in FORGES:
        name = f.bot_identity()
        if name:
            return name
    return ""


def list_reviewable_prs(login: str) -> list[dict]:
    """Open change requests this bot should review, most recent first.

    `login` is not passed on: each host resolves its own identity, because two
    hosts are two accounts and asking one of them about the other's login
    finds nothing. It stays in the signature because an empty one still means
    "we could not establish who we are", and reviewing as nobody would pick up
    every open change request in every permitted project.
    """
    if not login:
        return []
    return FORGES.reviewable_change_requests(MAX_PRS)


def repo_of(item: dict) -> str:
    """The repository a discovery record names."""
    return (item or {}).get("repo") or ""


def change_request(repo: str, number: int) -> dict:
    """The change request itself. Needed because no listing endpoint on any
    host carries the head commit the whole gate turns on."""
    return FORGES.of(repo).change_request(repo, number)


def head_check_state(repo: str, sha: str) -> str:
    """The gate for one head commit, as the host reduces it.

    The reduction lives in the forge and nowhere else — see forge.py on why
    every caller seeing raw payloads would re-derive it differently, and why
    `none` must not read as `pending`.
    """
    return FORGES.of(repo).checks_state(repo, sha)


# ---- pod-side queries -------------------------------------------------


def k8s_token() -> str:
    return _read(f"{K8S_SA_DIR}/token")


def k8s_namespace() -> str:
    return _read(f"{K8S_SA_DIR}/namespace")


def find_openclaw_pod(namespace: str) -> str:
    """Same labelSelector logic as the fixer and tester planners. Some
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


def pod_exec(namespace: str, pod: str, script: str, timeout: int = 20):
    return subprocess.run(
        ["kubectl", "-n", namespace, "exec", pod, "-c", "openclaw", "--",
         "bash", "-c", script],
        capture_output=True, text=True, timeout=timeout,
    )


def query_pod_state(namespace: str, pod: str) -> tuple[set[str], dict[str, str]]:
    """Return (locked repo keys, {<owner>__<name>__<number> → last-reviewed
    stamp}). The repo key is `owner__name`, matching the runner's encoding.

    A lock counts as held only while it is BOTH inside the TTL and owned by a
    live reviewer-runner process. A pod restart or a killed runner leaves the
    directory behind, and a planner that trusted it would strand the repo
    until someone cleaned up by hand — same PID-aware rule the fixer and
    tester planners use, and the same one reviewer-runner.sh applies when it
    decides whether to take a lock over.
    """
    if not pod:
        return (set(), {})

    script = (
        "set -eu; "
        "root=$HOME/.openclaw/.reviewer-locks; "
        f"now=$(date +%s); ttl={TTL_SECONDS}; "
        'echo "==LOCKS=="; '
        "if [ -d $root ]; then "
        "  for d in $(find $root -maxdepth 1 -mindepth 1 -type d 2>/dev/null); do "
        "    age=$(( now - $(stat -c %Y \"$d\") )); "
        "    [ $age -lt $ttl ] || continue; "
        "    pid=$(awk '{print $1; exit}' \"$d/owner\" 2>/dev/null || true); "
        "    [ -n \"$pid\" ] || continue; "
        "    [ -d \"/proc/$pid\" ] || continue; "
        "    grep -aq reviewer-runner \"/proc/$pid/cmdline\" 2>/dev/null || continue; "
        "    echo $(basename \"$d\"); "
        "  done; "
        "fi; "
        'echo "==REVIEWED=="; '
        "state=$HOME/.openclaw/reviewer-state; "
        "if [ -d $state ]; then "
        "  for f in $(find $state -maxdepth 1 -mindepth 1 -name '*.last-reviewed-sha' 2>/dev/null); do "
        '    base=$(basename "$f" .last-reviewed-sha); '
        '    stamp=$(cat "$f" 2>/dev/null); '
        "    echo \"$base $stamp\"; "
        "  done; "
        "fi"
    )
    try:
        proc = pod_exec(namespace, pod, script)
        if proc.returncode != 0:
            return (set(), {})
    except Exception:
        return (set(), {})

    locks: set[str] = set()
    reviewed: dict[str, str] = {}
    section = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line == "==LOCKS==":
            section = "locks"
            continue
        if line == "==REVIEWED==":
            section = "reviewed"
            continue
        if not line:
            continue
        if section == "locks":
            locks.add(line)
        elif section == "reviewed":
            # "<key> <sha> [<prose-digest>]" — keep everything after the key.
            # Taking the sha alone throws the digest away, which makes every
            # pull request look prose-unknown and re-reviewed once per tick.
            parts = line.split(None, 1)
            if len(parts) >= 2:
                reviewed[parts[0]] = parts[1].strip()
    return (locks, reviewed)


def query_permissions(namespace: str, pod: str,
                      repos: list[str]) -> tuple[dict[str, int], bool]:
    """Ask `project-allow check` about each repo, inside the pod that has the
    list on its PVC. Returns ({repo: exit code}, list-was-readable).

    Exit 2 is "not permitted" — the answer. Anything else (the CLI missing,
    the list unreadable, the exec failing) means we cannot tell what is
    permitted, which permits NOTHING: the same fail-closed rule the other
    planners use, reported so the spawner can say why nothing was reviewed
    rather than looking idle.
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
    if not FORGES:
        print(json.dumps({"error": "no code host is configured (set "
                                   "GITHUB_TOKEN, or GITLAB_URL and "
                                   "GITLAB_API_TOKEN)", "prs": []}))
        return
    namespace = k8s_namespace()
    pod = find_openclaw_pod(namespace)
    locks, reviewed = query_pod_state(namespace, pod)

    try:
        login = bot_login()
        items = list_reviewable_prs(login)
    except Exception as e:
        print(json.dumps({"namespace": namespace, "error": str(e), "prs": []}))
        return

    # Permission first — one exec for every repo in the result, ahead of the
    # per-pull-request fetches it would otherwise pay for. Being asked for a
    # review is a request, not an authorisation.
    repos = [r for r in {repo_of(i) for i in items} if r]
    perms, allowlist_available = query_permissions(namespace, pod, repos)

    # Most urgent first, then oldest. Sorted BEFORE the loop, not after: the
    # per-repo claim below is first-come and search hands results back by
    # updated_at, so without this a repo's single reviewer slot goes to
    # whichever pull request someone commented on most recently rather than
    # the most urgent one. Sorting the output alone would look right in the
    # plan and behave wrong.
    items = sorted(
        items,
        key=lambda i: (issue_priority.priority_of(i.get("labels")),
                       i.get("number") or 0),
    )

    out: list[dict] = []
    # One reviewer per repo per tick — the review checkout is shared, so two
    # runners on one repo would race in the same working tree.
    claimed: set[str] = set()
    for item in items:
        repo = repo_of(item)
        number = item.get("number")
        if not repo or not number:
            continue
        repo_key = repo.replace("/", "__")
        state_key = f"{repo_key}__{number}"
        entry: dict = {
            "repo": repo,
            "prNumber": number,
            "priority": issue_priority.label_for(
                issue_priority.priority_of(item.get("labels"))),
        }

        denied = permission_reason(perms.get(repo))
        if denied:
            entry.update(toSpawn=False, reason=denied)
            out.append(entry)
            continue
        if repo_key in locks or repo_key in claimed:
            entry.update(toSpawn=False, reason="lock-held")
            out.append(entry)
            continue

        pr = change_request(repo, number)
        if not pr:
            entry.update(toSpawn=False, reason="pr-fetch-failed")
            out.append(entry)
            continue
        entry.update({
            "headSha": pr.get("headSha") or "",
            "headRef": pr.get("headRef") or "",
            "baseRef": pr.get("baseRef") or "",
        })

        if (pr.get("state") or "") != "open":
            entry.update(toSpawn=False, reason="closed")
        elif pr.get("draft"):
            entry.update(toSpawn=False, reason="draft")
        elif review_subject.already_reviewed(
                reviewed.get(state_key), entry["headSha"],
                pr.get("title"), pr.get("body")):
            # Same commit AND same prose. Keyed on the sha alone, a verdict
            # about the description could never be cleared: the author edits
            # the body, the sha does not move, and nothing looks again. See
            # review_subject.py.
            entry.update(toSpawn=False, reason="already-reviewed")
        else:
            state = head_check_state(repo, entry["headSha"])
            if state in (GREEN, NONE):
                # NONE is a repository with no CI at all. Waiting for checks
                # that will never be created would strand every pull request
                # in it forever, so review it — and pay, at worst, one wasted
                # run when a check registers a moment later.
                entry.update(toSpawn=True, checks=state)
                claimed.add(repo_key)
            elif state == FAILED:
                # Red head: nothing to review — the author (solver or human)
                # has to fix CI first. The solver only requests a review after
                # green, so this is mostly the human path.
                entry.update(toSpawn=False, reason="checks-failed")
            else:
                entry.update(toSpawn=False, reason="wait-checks")
        out.append(entry)

    out.sort(key=lambda p: (
        0 if p.get("toSpawn") else 1,
        issue_priority.LEVELS.get(
            issue_priority._normalise(p.get("priority", "")),
            issue_priority.DEFAULT_LEVEL),
        p.get("prNumber", 0),
    ))

    print(json.dumps({
        "namespace": namespace,
        "botLogin": login,
        "prs": out,
        "pendingReviews": len(out),
        "allowlistAvailable": allowlist_available,
    }))


if __name__ == "__main__":
    main()

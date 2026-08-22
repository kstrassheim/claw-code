#!/bin/bash
# cron-reviewer-spawn: invoked by the `pr-reviewer` CronJob every tick.
#
# Calls the read-only reviewer-tick planner to produce a JSON plan, then
# for each entry with toSpawn=true kubectl-exec's a reviewer-runner
# subprocess into the openclaw pod. The runner does its own thing
# (per-repo lock, checkout of the pull request's head branch, local
# validation, verdict comment + review) and exits.
#
# Concurrency lives entirely in the openclaw container's filesystem:
# per-repo lock dir under ~/.openclaw/.reviewer-locks/<owner>__<name>/.
# This script does NOT decide whether to spawn — it only translates
# the planner's output into kubectl invocations.
#
# The schedule (2-59/5) is deliberately offset from the issue-watcher
# (*/5) and the tester (*/10) so the three planners never fire in the
# same minute and race each other for a model slot.
set -euo pipefail

NAMESPACE=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)

# Which app label the gateway pod carries. Defaults to `openclaw`, which is what
# this chart has always used; a deployment that names its Deployment something
# else sets OPENCLAW_APP_LABEL in the CronJob env. Hardcoding it meant the
# spawner could only find a pod in a namespace where the Deployment happened to
# be called openclaw — anywhere else every tick died on "no Running openclaw pod
# found" before doing any work.
APP_LABEL="${OPENCLAW_APP_LABEL:-claw-code}"

OPENCLAW_POD=$(kubectl -n "$NAMESPACE" get pod \
    -l "app=$APP_LABEL,component=server" \
    -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' \
    | awk '{print $1}')
if [ -z "$OPENCLAW_POD" ]; then
  OPENCLAW_POD=$(kubectl -n "$NAMESPACE" get pod \
      -l "app=$APP_LABEL" \
      -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' \
      | awk '{print $1}')
fi
test -n "$OPENCLAW_POD" || { echo "ERROR: no Running pod with app=$APP_LABEL found in $NAMESPACE" >&2; exit 1; }
export OPENCLAW_POD
echo "openclaw pod: $OPENCLAW_POD"

# WHY THESE ARE NOT ABSOLUTE PATHS.
#
# Everything under builder/ ships twice — baked into the image at
# /usr/local/bin, and generated into ConfigMaps that mount at
# /opt/claw-scripts, which comes FIRST on PATH so an edit reaches the cluster
# without an image rebuild. That is the whole point of the ConfigMap.
#
# Naming the image path here defeated it silently. The mount was present, the
# file in it was current, and nothing ever executed it: every runner and tick
# was spawned as /usr/local/bin/<name>, so a script edit shipped by ConfigMap
# sat there being correct and unread until the next version bump. Two fixes
# landed that way, and the only symptom was that the bot kept doing the old
# thing.
#
# Everything here is named WITHOUT a directory, so PATH decides — and PATH puts
# the mount first in both the cron pods and the gateway (see 020-deployment and
# 050-issue-watcher). A ConfigMap that fails to mount therefore degrades to the
# image copy, which is stale but present, rather than to "command not found".
#
# A bare name is also the only form that survives this file: the spawn command
# is built inside a double-quoted `python3 -c "..."`, where a `$VAR` would be
# expanded by the shell before Python ever saw it and a `"` would end the
# string outright.
PLAN=$(reviewer-tick)
echo "$PLAN" | python3 -c "
import json, os, subprocess, sys, shlex
plan = json.load(sys.stdin)

OPENCLAW_POD = os.environ['OPENCLAW_POD']
# Same default as the shell above. Read from the environment rather than
# interpolated, so the value cannot differ between the two halves of this file.
CONTAINER = os.environ.get('OPENCLAW_CONTAINER', 'claw-code')

# Error first: the earliest planner failures answer before they know the
# namespace, so reading it up front would turn a clear message into a KeyError.
if plan.get('error'):
    print(f\"planner error: {plan['error']}\", file=sys.stderr)
    sys.exit(1)
NAMESPACE = plan['namespace']

spawned = 0
skipped = 0
for p in plan.get('prs', []):
    if not p.get('toSpawn'):
        skipped += 1
        continue
    repo = p['repo']
    number = p['prNumber']

    # setsid + redirected stdio detach the reviewer-runner from the
    # kubectl-exec connection so it survives past this script's exit
    # (otherwise it would get SIGHUP'd the moment the exec closes).
    runner_args = ' '.join(shlex.quote(str(a)) for a in
                           [repo, number, p.get('headRef',''), p.get('headSha','')])
    remote_cmd = (
        f'setsid bash -c '
        + shlex.quote(f'nohup reviewer-runner {runner_args} >/dev/null 2>&1 </dev/null &')
        + ' >/dev/null 2>&1 </dev/null &'
    )
    proc = subprocess.run(
        ['kubectl', '-n', NAMESPACE, 'exec', OPENCLAW_POD, '-c', CONTAINER,
         '--', 'bash', '-c', remote_cmd],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        print(f'ERROR exec for review {repo}#{number}: rc={proc.returncode} stderr={proc.stderr.strip()}', file=sys.stderr)
    else:
        print(f'spawned reviewer for {repo}#{number} @ {p.get(\"headSha\",\"\")[:7]}')
        spawned += 1

denied = sorted({p['repo'] for p in plan.get('prs', [])
                 if str(p.get('reason','')) == 'not-permitted'})
if plan.get('allowlistAvailable') is False:
    print('WARNING: could not read the allowed-projects list — nothing was reviewed. '
          'Check the openclaw pod and ~/.openclaw/projects-allowed.list', file=sys.stderr)
elif denied:
    print(f'not permitted ({len(denied)}): ' + ', '.join(denied[:10]))

waiting = [p for p in plan.get('prs', []) if p.get('reason') == 'wait-checks']
red = [p for p in plan.get('prs', []) if p.get('reason') == 'checks-failed']
if waiting:
    print(f'waiting on checks ({len(waiting)}): '
          + ', '.join(f\"{p['repo']}#{p['prNumber']}\" for p in waiting[:10]))
if red:
    print(f'checks failed, not reviewing ({len(red)}): '
          + ', '.join(f\"{p['repo']}#{p['prNumber']}\" for p in red[:10]))
print(f'reviewer tick done: spawned={spawned}, skipped={skipped}, '
      f'pending={plan.get(\"pendingReviews\")}')
"

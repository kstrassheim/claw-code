#!/bin/bash
# cron-tester-spawn: invoked by the `tester` CronJob every tick.
#
# Calls the read-only tester-tick planner to produce a JSON plan,
# then for each entry with toSpawn=true kubectl-exec's a tester-runner
# subprocess into the openclaw pod. The runner does its own thing
# (per-repo lock, last-head-changed gate, agent invocation, draft
# processing, issue creation) and exits.
#
# Concurrency lives entirely in the openclaw container's filesystem:
# per-repo lock dir under ~/.openclaw/.tester-locks/<owner>__<name>/.
# This script does NOT decide whether to spawn — it only translates
# the planner's output into kubectl invocations.
#
# The schedule (4-59/10) is deliberately offset from the issue-watcher
# (*/5) and the pull-request reviewer (2-59/5) so no two planners fire
# in the same minute and race each other for a model slot.
#
# REPORTING IS PART OF THE JOB. A tick that spawned nothing has to say
# WHICH of the several reasons it was: held back behind the solver's or
# the reviewer's queue, denied by the allowed-projects list, or unable
# to read that list at all. The generic tail line alone
# ("spawned=0, skipped=0") reads like a broken allowlist in every one of
# those cases, and sends whoever is on call to look at the wrong thing.
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
PLAN=$(tester-tick)
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

# Held back on purpose: the issue solver or the reviewer still has work, and
# testing waits for both to drain ('first solve and merge, then test'). Say so
# explicitly, WITH the queue detail — otherwise this tick is indistinguishable
# from an empty allowlist, which is a completely different thing to fix.
if plan.get('skipped'):
    q = plan.get('queues') or {}
    detail = ', '.join(f'{k}={v}' for k, v in sorted(q.items()))
    print(f\"tester held back — {plan['skipped']}\" + (f' [{detail}]' if detail else ''))
    sys.exit(0)

spawned = 0
skipped = 0
for r in plan.get('repos', []):
    if not r.get('toSpawn'):
        skipped += 1
        continue
    repo = r['repo']

    # Build the exec command. setsid + redirected stdio detach the
    # tester-runner from the kubectl-exec connection so it survives
    # past this script's exit (otherwise it would get SIGHUP'd).
    runner_args = shlex.quote(repo)
    remote_cmd = (
        f'setsid bash -c '
        + shlex.quote(f'nohup tester-runner {runner_args} >/dev/null 2>&1 </dev/null &')
        + ' >/dev/null 2>&1 </dev/null &'
    )
    proc = subprocess.run(
        ['kubectl', '-n', NAMESPACE, 'exec', OPENCLAW_POD, '-c', CONTAINER,
         '--', 'bash', '-c', remote_cmd],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        print(f'ERROR exec for tester {repo}: rc={proc.returncode} stderr={proc.stderr.strip()}', file=sys.stderr)
    else:
        head = r.get('headSha','')[:7]
        prior = (r.get('priorHead') or '')[:7] or 'none'
        print(f'spawned tester for {repo}: {prior} -> {head}')
        spawned += 1

# Allowlist denials, named. 'not-permitted' is an answer (the owner did not
# grant this repository); 'allowlist-unavailable' means we could not ask, and
# is a fault to fix rather than a decision to respect.
denied = sorted({r['repo'] for r in plan.get('repos', [])
                 if str(r.get('reason','')) == 'not-permitted'})
if plan.get('allowlistAvailable') is False:
    print('tester held back — could not read the allowed-projects list, so nothing '
          'was tested. Check the openclaw pod and ~/.openclaw/projects-allowed.list',
          file=sys.stderr)
elif not plan.get('allowedProjects'):
    print('tester held back — no repositories are permitted yet '
          '(grant one from chat with: projects add <owner>/<repo>)')
elif denied:
    print(f'not permitted ({len(denied)}): ' + ', '.join(denied[:10]))

unchanged = [r['repo'] for r in plan.get('repos', [])
             if r.get('reason') == 'head-unchanged']
locked = [r['repo'] for r in plan.get('repos', [])
          if r.get('reason') == 'lock-held']
if unchanged:
    print(f'already tested at current HEAD ({len(unchanged)}): ' + ', '.join(unchanged[:10]))
if locked:
    print(f'tester already in flight ({len(locked)}): ' + ', '.join(locked[:10]))
print(f'tester tick done: spawned={spawned}, skipped={skipped}, '
      f'permitted={plan.get(\"allowedProjects\")}')
"

#!/bin/bash
# cron-issue-spawn: invoked by the issue-watcher CronJob every tick.
#
# Calls the (read-only) tick planner to produce a JSON spawn plan, then
# for each entry kubectl-exec's into the openclaw pod and backgrounds
# `fixer-runner` there — by NAME, so PATH picks the ConfigMap copy over the
# image's; see the note above the plan below. The fixer runs as a subprocess
# inside the openclaw container — it shares the pod's network, secrets,
# config, and persistent workspace volume (so it can keep a long-lived
# git checkout under ~/.openclaw/projects/<repo>/).
#
# Concurrency lives in the openclaw container's filesystem: one mkdir
# lock per repo, max 1 fixer per repo. Fewer than 2 (the previous cap)
# because two subprocesses can't safely share the same on-disk
# checkout. Issues queued for a busy repo wait for the next tick.
#
# This script does NOT decide what to spawn. It only translates the
# planner's `toSpawn` array into kubectl-exec invocations.
set -euo pipefail

NAMESPACE=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)

# Which app label the gateway pod carries. Defaults to `openclaw`, which is what
# this chart has always used; a deployment that names its Deployment something
# else sets OPENCLAW_APP_LABEL in the CronJob env. Hardcoding it meant the
# spawner could only find a pod in a namespace where the Deployment happened to
# be called openclaw — anywhere else every tick died on "no Running openclaw pod
# found" before doing any work.
APP_LABEL="${OPENCLAW_APP_LABEL:-claw-code}"

# The container inside that pod. Same story as APP_LABEL: it was hardcoded to
# `openclaw`, so once the pod was FOUND the exec still failed —
#
#     error: container openclaw is not valid for pod claw-code-...
#     out of: claw-code, fix-perms (init), render-config (init)
#
# Both deployments now name the container claw-code, so that is the default.
CONTAINER="${OPENCLAW_CONTAINER:-claw-code}"

# Resolve the running openclaw pod once per tick.
OPENCLAW_POD=$(kubectl -n "$NAMESPACE" get pod \
    -l "app=$APP_LABEL,component=server" \
    -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' \
    | awk '{print $1}')
if [ -z "$OPENCLAW_POD" ]; then
  # Some deployments don't carry the component=server label.
  OPENCLAW_POD=$(kubectl -n "$NAMESPACE" get pod \
      -l "app=$APP_LABEL" \
      -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' \
      | awk '{print $1}')
fi
test -n "$OPENCLAW_POD" || { echo "ERROR: no Running pod with app=$APP_LABEL found in $NAMESPACE" >&2; exit 1; }
export OPENCLAW_POD
echo "openclaw pod: $OPENCLAW_POD"

# -- per-tick side jobs, all non-fatal --------------------------------
#
# Three chores that need to run OFTEN and inside the openclaw pod, where the
# token, the model credentials and the planning store's connection live. This
# tick already fires every five minutes and already execs into that pod, so
# they ride along instead of becoming three more CronJobs to keep alive.
#
# Every one of them is tolerated as non-fatal, and that is the point rather
# than laziness: they are bookkeeping ABOUT the work, and the tick's actual
# job is spawning the work. Nothing here may become a reason the issue solver
# stops running.

# Quota / rate-limit watch. Reads the runners' own logs, which is why it runs
# in the pod that has them.
kubectl -n "$NAMESPACE" exec "$OPENCLAW_POD" -c "$CONTAINER" -- \
    llm-quota --check >/dev/null 2>&1 || true

# Roll the sprint over if a scheduled boundary has passed.
#
# Asked every tick rather than scheduled at the boundary itself: a job firing
# exactly at the boundary minute misses the rollover COMPLETELY if the pod
# happens to be restarting then, and nothing would say so — the sprint would
# never end, the next would never begin, and the first symptom would be
# numbers that quietly stopped adding up. Asking every tick makes a late
# rollover normal and self-healing. Idempotent: it rolls only when the running
# sprint started before the most recent boundary, so a hundred ticks in a row
# do nothing.
kubectl -n "$NAMESPACE" exec "$OPENCLAW_POD" -c "$CONTAINER" -- \
    planning sprint-tick 2>&1 | grep -v '^$' || true

# Record stories whose pull request has been merged, WHOEVER merged it.
#
# `mergedAt` is written by the solver's exit trap, which only runs if a solver
# run happens after the merge — so a merge performed by a person is invisible
# to every report built on that field. This sweep fills the gap; it only ever
# fills an EMPTY field and never corrects one.
kubectl -n "$NAMESPACE" exec "$OPENCLAW_POD" -c "$CONTAINER" -- \
    record-deliveries 2>&1 | grep -v '^$' || true

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
PLAN=$(heartbeat-issue-tick)
echo "$PLAN" | python3 -c "
import json, os, subprocess, sys, shlex
plan = json.load(sys.stdin)

OPENCLAW_POD = os.environ['OPENCLAW_POD']
# Same default as the shell above. Read from the environment rather than
# interpolated, so the value cannot differ between the two halves of this file.
CONTAINER = os.environ.get('OPENCLAW_CONTAINER', 'claw-code')
NAMESPACE = plan['namespace']

errors = [r for r in plan['repos'] if r.get('error')]
for e in errors:
    print(f\"ERROR {e['repo']}: {e['error']}\", file=sys.stderr)

spawned = 0
for r in plan['repos']:
    if r.get('error'):
        continue
    for issue in r.get('toSpawn', []):
        repo = r['repo']
        n = issue['issueNumber']
        url = issue['url']
        title = issue['title']

        # Size BEFORE implementation. An issue with no estimate is ESTIMATED
        # this tick and implemented on a later one, because the model the
        # solver gets is chosen from the size — sizing during the run that
        # already picked a model would be circular.
        #
        # Costing a tick is deliberate and cheap: ticks are five minutes
        # apart, the estimate is one short model call, and the delay leaves a
        # window in which a human can overrule the number before any code is
        # written.
        if issue.get('needsEstimate'):
            runner = 'estimate-runner'
            runner_args = ' '.join(shlex.quote(a) for a in [repo, str(n)])
            env_prefix = ''
            what = 'estimate'
        else:
            runner = 'fixer-runner'
            runner_args = ' '.join(shlex.quote(a) for a in [repo, str(n), url, title])
            # The size travels to the runner, which picks solver vs
            # solver.small from it. Absent means the solver defaults to 8 —
            # the strong model — which is the safe direction.
            env_prefix = 'STORY_POINTS=%s ' % shlex.quote(
                str(issue.get('storyPoints') or ''))
            what = 'fixer'

        # Build the exec command. setsid + redirected stdio detach the
        # runner from the kubectl-exec connection so it survives
        # past this script's exit (otherwise it would get SIGHUP'd).
        # Args are shell-escaped to survive the bash-c wrapper.
        remote_cmd = (
            f'setsid bash -c '
            + shlex.quote(f'nohup env {env_prefix}{runner} {runner_args} '
                          f'>/dev/null 2>&1 </dev/null &')
            + ' >/dev/null 2>&1 </dev/null &'
        )

        proc = subprocess.run(
            ['kubectl', '-n', NAMESPACE, 'exec', OPENCLAW_POD, '-c', CONTAINER,
             '--', 'bash', '-c', remote_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            print(f'ERROR exec for {repo}#{n}: rc={proc.returncode} stderr={proc.stderr.strip()}', file=sys.stderr)
        else:
            print(f'spawned {what} for {repo}#{n}: {title}')
            spawned += 1

deferred = sum(r.get('deferredDueToLimit', 0) for r in plan['repos'])
# Issues a person put off to the next sprint. Counted here for the same
# reason the other gates are: a tick that spawned nothing has to say which
# gate stopped it rather than looking idle.
next_sprint = sum(r.get('nextSprint', 0) for r in plan['repos'])

# Why a repository was skipped, in the planner's own vocabulary. The three
# reasons are a contract, not prose: 'allowlist-unavailable' is a fault to
# fix, 'allowlist-empty' and 'not-permitted' are the owner's decision, and a
# tick that spawned nothing must be able to say which of the three it was
# instead of merely looking idle.
denied = [r['repo'] for r in plan['repos']
          if str(r.get('reason', '')).startswith(('not-permitted', 'allowlist-'))]
if plan.get('allowlistAvailable') is False:
    print('WARNING: could not read the allowed-projects list — no issue was picked up. '
          'Check the openclaw pod and ~/.openclaw/projects-allowed.list', file=sys.stderr)
elif denied:
    print(f'not permitted ({len(denied)}): ' + ', '.join(denied[:10]))
print(f'tick done: spawned={spawned}, deferred_due_to_limit={deferred}, '
      f'next_sprint={next_sprint}, '
      f'permitted={plan.get(\"allowedProjects\")}')
"

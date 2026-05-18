#!/bin/bash
# cron-issue-spawn: invoked by the issue-watcher CronJob every tick.
#
# Calls the (read-only) tick planner to produce a JSON spawn plan, then
# for each entry kubectl-exec's into the claw-code (openclaw) pod and
# backgrounds /usr/local/bin/fixer-runner.sh there. The fixer runs as a
# subprocess inside the main container — it shares the pod's network,
# secrets, config, and persistent workspace volume (so it can keep a
# long-lived git checkout under ~/.openclaw/projects/<repo>/).
#
# Concurrency lives in the pod's filesystem: one mkdir lock per repo,
# max 1 fixer per repo (two subprocesses can't safely share the same
# on-disk checkout). Issues queued for a busy repo wait for the next
# tick.
set -euo pipefail

NAMESPACE=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)

# Resolve the running main pod (Deployment.metadata.name == "claw-code"
# and the pod selector is `app=claw-code`, not `app=openclaw` — that
# label was renamed when the namespace was renamed).
OPENCLAW_POD=$(kubectl -n "$NAMESPACE" get pod \
    -l app=claw-code \
    -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' \
    | awk '{print $1}')
test -n "$OPENCLAW_POD" || { echo "ERROR: no Running claw-code pod found in $NAMESPACE" >&2; exit 1; }
export OPENCLAW_POD
echo "claw-code pod: $OPENCLAW_POD"

PLAN=$(/usr/local/bin/heartbeat-issue-tick)
echo "$PLAN" | python3 -c "
import json, os, subprocess, sys, shlex
plan = json.load(sys.stdin)

OPENCLAW_POD = os.environ['OPENCLAW_POD']
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

        runner_args = ' '.join(shlex.quote(a) for a in [repo, str(n), url, title])
        remote_cmd = (
            f'setsid bash -c '
            + shlex.quote(f'nohup /usr/local/bin/fixer-runner {runner_args} >/dev/null 2>&1 </dev/null &')
            + ' >/dev/null 2>&1 </dev/null &'
        )

        # Container name inside the pod is 'claw-code' (not 'openclaw').
        proc = subprocess.run(
            ['kubectl', '-n', NAMESPACE, 'exec', OPENCLAW_POD, '-c', 'claw-code',
             '--', 'bash', '-c', remote_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            print(f'ERROR exec for {repo}#{n}: rc={proc.returncode} stderr={proc.stderr.strip()}', file=sys.stderr)
        else:
            print(f'spawned fixer for {repo}#{n}: {title}')
            spawned += 1

deferred = sum(r.get('deferredDueToLimit', 0) for r in plan['repos'])
print(f'tick done: spawned={spawned}, deferred_due_to_limit={deferred}')
"

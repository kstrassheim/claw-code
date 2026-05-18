#!/bin/bash
# cron-issue-spawn: invoked by the issue-watcher CronJob every tick.
#
# Calls the (read-only) tick planner to produce a JSON spawn plan, then
# for each entry creates a K8s Job that runs `openclaw agent --local`
# against the issue. Idempotent — the planner enforces the cap based on
# already-running Jobs, and `kubectl create` uses a per-tick timestamp
# suffix to keep the Job name unique within the 63-char DNS-1123 limit.
#
# This script does NOT decide what to spawn. It only translates the
# planner's `toSpawn` array into Job manifests. Decision logic lives in
# heartbeat-issue-tick.py.
#
# Project-specific values are hardcoded for claw-code: the spawned
# Jobs mount claw-code-secrets + claw-code-config-template, and rely on
# AKS's managed-identity ACR pull (no imagePullSecrets, no nodeSelector,
# no tolerations). The claw-code-local sibling has its own copy with
# the on-prem k3s placement bits.
set -euo pipefail

# Resolve our own container image so spawned fixer Jobs run the same
# tag. The downward API can't return spec.containers[].image as a
# scalar, so look it up via the K8s API using POD_NAME (downward-API
# injected by the CronJob spec).
NAMESPACE=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
ISSUE_FIXER_IMAGE=$(kubectl -n "$NAMESPACE" get pod "$POD_NAME" \
    -o jsonpath='{.spec.containers[?(@.name=="watcher")].image}')
export ISSUE_FIXER_IMAGE
test -n "$ISSUE_FIXER_IMAGE" || { echo "ERROR: could not resolve ISSUE_FIXER_IMAGE for $POD_NAME" >&2; exit 1; }

PLAN=$(/usr/local/bin/heartbeat-issue-tick)
echo "$PLAN" | python3 -c "
import json, os, subprocess, sys, time
plan = json.load(sys.stdin)

IMAGE = os.environ['ISSUE_FIXER_IMAGE']
TTL_SECONDS = int(os.environ.get('JOB_AGENT_TIMEOUT_SECONDS', '3600'))
ACTIVE_DEADLINE = TTL_SECONDS + 100  # K8s kills if agent doesn't self-exit
NAMESPACE = plan['namespace']

errors = [r for r in plan['repos'] if r.get('error')]
for e in errors:
    print(f\"ERROR {e['repo']}: {e['error']}\", file=sys.stderr)

spawned = 0
for r in plan['repos']:
    if r.get('error'):
        continue
    slug = r['slug']
    for issue in r.get('toSpawn', []):
        n = issue['issueNumber']
        # Names must be <= 63 chars and DNS-1123. Slug already lowercase
        # with dots; we replace dots with dashes for the Job name.
        name_slug = slug.replace('.', '-')
        ts = int(time.time())
        job_name = f'fix-{name_slug}-{n}-{ts}'
        job_name = job_name[:63]

        message = (
            f\"Fix GitHub issue {issue['url']} end-to-end. Use the gh-issues skill if helpful. \"
            f\"Steps: (1) clone {r['repo']} into a temp dir, (2) create a feature branch \"
            f\"issue-{n}-fix, (3) implement the change, (4) commit with a descriptive message, \"
            f\"(5) push the branch, (6) open a PR back to the source repo's default branch \"
            f\"with 'Closes #{n}' in the body, then stop. Do not delegate to subagents. \"
            f\"Do not ask the user for confirmation. Use cameron-claw as the git author.\"
        )

        manifest = {
            'apiVersion': 'batch/v1',
            'kind': 'Job',
            'metadata': {
                'name': job_name,
                'namespace': NAMESPACE,
                'labels': {
                    'app': 'issue-fixer',
                    'issueRepo': slug,
                    'issueNumber': str(n),
                },
            },
            'spec': {
                'ttlSecondsAfterFinished': 600,
                'activeDeadlineSeconds': ACTIVE_DEADLINE,
                'backoffLimit': 0,
                'template': {
                    'metadata': {
                        'labels': {
                            'app': 'issue-fixer',
                            'issueRepo': slug,
                            'issueNumber': str(n),
                        }
                    },
                    'spec': {
                        'restartPolicy': 'Never',
                        'serviceAccountName': 'issue-watcher',
                        'containers': [{
                            'name': 'agent',
                            'image': IMAGE,
                            # K8s default: Always for :latest, IfNotPresent
                            # otherwise. Both are fine for our tags.
                            'command': [
                                'openclaw', 'agent', '--local',
                                '--timeout', str(TTL_SECONDS),
                                '--session-id', f'issue-{slug}-{n}',
                                '--message', message,
                            ],
                            'envFrom': [{'secretRef': {'name': 'claw-code-secrets'}}],
                            'env': [
                                {'name': 'KNOWLEDGE_BOT_ISSUE_URL', 'value': issue['url']},
                            ],
                            # Mount the openclaw config template directly:
                            # the rendered claw-code-config ConfigMap is
                            # currently empty due to a render-config.sh
                            # namespace bug (writes to ns 'openclaw'), so
                            # the template is the only reliable source of
                            # provider definitions. The init container
                            # copies it into the agent's workspace dir.
                            'volumeMounts': [
                                {'name': 'workspace', 'mountPath': '/home/node/.openclaw'},
                                {'name': 'config', 'mountPath': '/template'},
                            ],
                            'resources': {
                                'requests': {'cpu': '200m', 'memory': '512Mi'},
                                'limits': {'cpu': '1500m', 'memory': '3Gi'},
                            },
                        }],
                        'initContainers': [{
                            'name': 'render-config',
                            'image': IMAGE,
                            'command': ['/bin/sh', '-c',
                                'mkdir -p /home/node/.openclaw && '
                                'cp /template/openclaw.json /home/node/.openclaw/openclaw.json'],
                            'volumeMounts': [
                                {'name': 'workspace', 'mountPath': '/home/node/.openclaw'},
                                {'name': 'config', 'mountPath': '/template'},
                            ],
                        }],
                        'volumes': [
                            {'name': 'workspace', 'emptyDir': {}},
                            {'name': 'config', 'configMap': {'name': 'claw-code-config-template'}},
                        ],
                    }
                }
            }
        }

        proc = subprocess.run(
            ['kubectl', 'apply', '-f', '-'],
            input=json.dumps(manifest),
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f'ERROR spawning {job_name}: {proc.stderr}', file=sys.stderr)
        else:
            print(f'spawned {job_name} for {r[\"repo\"]}#{n}: {issue[\"title\"]}')
            spawned += 1

print(f'tick done: spawned={spawned}, deferred_due_to_limit={sum(r.get(\"deferredDueToLimit\", 0) for r in plan[\"repos\"])}')
"

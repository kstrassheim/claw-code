# The CronJob image: python3 + kubectl, and nothing else.
#
# WHY THIS EXISTS
# The three agent CronJobs (issue-watcher, pr-reviewer, tester) are
# ORCHESTRATORS, not agents. Each one finds the running openclaw pod and does
# the real work inside it via `kubectl exec` — the agent runtime, the model
# calls, git, the language toolchains and the scanners all live over there.
# What runs HERE is one stdlib-only Python planner and a handful of kubectl
# calls.
#
# They were running from the full agent image: 1.83 GB pulled every time a new
# tag lands, on three schedules that fire every 5, 5 and 10 minutes, to execute
# a workload whose measured peak is 18 MiB of Python plus 39 MiB of kubectl.
# None of node, the openclaw runtime, terraform, tofu, the cloud CLIs, the .NET
# SDK, PowerShell, semgrep, nuclei or code-server is reachable from these
# entrypoints.
#
# WHAT MUST STAY TRUE
# The file list below is not decoration: it is the transitive closure of what
# the three entrypoints import and exec, and `tests/test_cron_image.py` derives
# that closure again from the source and fails if this file has drifted from
# it. Adding an import to a tick script without adding the module here would
# otherwise fail at 00:05 in a CronJob nobody is watching, not in CI.
#
# ca-certificates is required, not optional: forge.py talks to the code host
# over TLS with urllib and its own ssl context, and an image without a trust
# store fails every API call with a verification error.
FROM python:3.12-alpine

ARG TARGETARCH
RUN set -eux; \
    apk add --no-cache ca-certificates bash curl; \
    arch="${TARGETARCH:-arm64}"; \
    KUBECTL_VERSION="$(curl -sfL https://dl.k8s.io/release/stable.txt)"; \
    curl -sfL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl"; \
    chmod 0755 /usr/local/bin/kubectl; \
    kubectl version --client=true >/dev/null; \
    apk del curl

# Entrypoints — installed suffix-stripped and executable, exactly as the agent
# image does it, so the CronJob `command:` is identical in both.
COPY --chmod=0755 cron-issue-spawn.sh     /usr/local/bin/cron-issue-spawn
COPY --chmod=0755 cron-tester-spawn.sh    /usr/local/bin/cron-tester-spawn
COPY --chmod=0755 cron-reviewer-spawn.sh  /usr/local/bin/cron-reviewer-spawn
COPY --chmod=0755 heartbeat-issue-tick.py /usr/local/bin/heartbeat-issue-tick
COPY --chmod=0755 tester-tick.py          /usr/local/bin/tester-tick
COPY --chmod=0755 reviewer-tick.py        /usr/local/bin/reviewer-tick

# Modules they import. 0644 like the agent image — imported, never executed.
COPY --chmod=0644 forge.py               /usr/local/bin/forge.py
COPY --chmod=0644 forge_github.py        /usr/local/bin/forge_github.py
COPY --chmod=0644 forge_gitlab.py        /usr/local/bin/forge_gitlab.py
COPY --chmod=0644 forge_gitea.py         /usr/local/bin/forge_gitea.py
COPY --chmod=0644 forge_azdo.py          /usr/local/bin/forge_azdo.py
COPY --chmod=0644 issue_priority.py      /usr/local/bin/issue_priority.py
COPY --chmod=0644 issue_status.py        /usr/local/bin/issue_status.py
COPY --chmod=0644 approval_release.py    /usr/local/bin/approval_release.py
COPY --chmod=0644 lexical_guard.py       /usr/local/bin/lexical_guard.py
COPY --chmod=0644 project_allowlist.py   /usr/local/bin/project_allowlist.py
COPY --chmod=0644 queue_state.py         /usr/local/bin/queue_state.py
COPY --chmod=0644 review_subject.py      /usr/local/bin/review_subject.py
COPY --chmod=0644 story_estimate.py      /usr/local/bin/story_estimate.py
COPY --chmod=0644 story_points.py        /usr/local/bin/story_points.py

# The ticks import by bare name, the same way they do in the agent image.
ENV PYTHONPATH=/usr/local/bin

# Nothing here needs root, and this image holds a token: the CronJobs mount
# openclaw-secrets for GITHUB_TOKEN.
#
# NOT named `cron`: alpine already ships a `cron` system user (uid 16), and
# `adduser -D -u 1000 cron` fails with "user 'cron' in use". The name is
# arbitrary; the uid is what the cluster cares about, and 1000 matches the
# agent image so a shared volume keeps the same ownership either side.
RUN adduser -D -u 1000 claw
USER 1000

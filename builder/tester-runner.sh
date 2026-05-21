#!/bin/bash
# tester-runner: backgrounded subprocess inside the openclaw container.
# Per-repo, per-commit website + pipeline tester that runs in parallel
# to the fixer-runner. Different role: it READS and TESTS, never writes
# code or opens PRs.
#
# Per cron tick (typically every 10 min, see k8s/051-tester.yaml):
#   1. Resolve the bot identity from $GITHUB_TOKEN (same trick as
#      fixer-runner).
#   2. Compare GitHub's current main HEAD for this repo against the
#      saved last-tested HEAD at $STATE_ROOT/tester-state/<repo>.last-head.
#      Same → exit silently (no chat post per spec).
#      Different → proceed.
#   3. Acquire a per-repo tester lock so two tester subprocesses
#      can't race on the same repo (same mkdir-atomic pattern as the
#      fixer's lock, but a separate dir so the two subsystems don't
#      block each other).
#   4. Clone/update the repo into $STATE_ROOT/tester-projects/<repo>/
#      (separate from $STATE_ROOT/projects/, which the fixer uses).
#   5. Run `openclaw agent --local` with the TESTER prompt — distinct
#      from the fixer prompt. The agent only stages issue drafts as
#      JSON files in $DRAFTS_DIR; it does NOT create issues itself.
#   6. After agent exits, the wrapper reads drafts and creates the
#      GitHub issues, substituting the right assignee (BOT for things
#      the issue-solver can fix; OWNER for things needing the human).
#   7. Mark the HEAD as tested, post a short result summary, exit.
#
# Args:
#   $1 repo full_name   (owner/name)
#
# Required env:
#   GITHUB_TOKEN        bot PAT (already on the openclaw pod)
#   ENTRA_*             only if the deployed site is Entra-protected
#                       (the agent reads these via TOOLS-entra.md)
#
# Optional env:
#   TESTER_BOT_LOGIN    bot's GH login. If unset, resolved at startup.
#   TESTER_MAX_LIFETIME overall wall-clock cap, seconds (default 3600).
set -uo pipefail

REPO="${1:?repo full_name required (owner/name)}"

# Resolve bot identity. Pinned identity (env var) wins; otherwise look it
# up. Hardcoding would couple the code to one deployment's identity —
# different clusters use different bot accounts.
if [ -n "${TESTER_BOT_LOGIN:-}" ]; then
  BOT_LOGIN="$TESTER_BOT_LOGIN"
else
  BOT_LOGIN="$(curl -fsSL \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    https://api.github.com/user 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('login',''))" \
    2>/dev/null)"
  if [ -z "$BOT_LOGIN" ]; then
    echo "FATAL: tester cannot resolve bot identity from \$GITHUB_TOKEN /user" >&2
    exit 1
  fi
fi

# Repo owner — used as the OWNER @-mention / assignee target for issues
# the bot can't act on (e.g. Entra access denied). Derived from $REPO
# so no API call needed; identity-agnostic.
REPO_OWNER="${REPO%%/*}"

MAX_LIFETIME_SECONDS="${TESTER_MAX_LIFETIME:-3600}"
AGENT_TURN_TIMEOUT=3000

STATE_ROOT="${HOME:-/home/node}/.openclaw"
TESTER_PROJECTS_ROOT="$STATE_ROOT/tester-projects"
PROJECT_DIR="$TESTER_PROJECTS_ROOT/$REPO"
LOCK_ROOT="$STATE_ROOT/.tester-locks"
LOCK_DIR="$LOCK_ROOT/${REPO//\//__}"
LOG_DIR="$STATE_ROOT/tester-logs"
LOG_FILE="$LOG_DIR/${REPO//\//_}.log"
TESTER_STATE_DIR="$STATE_ROOT/tester-state"
LAST_HEAD_FILE="$TESTER_STATE_DIR/${REPO//\//__}.last-head"
TESTER_DRAFTS_ROOT="$STATE_ROOT/tester-drafts"
SUMMARIES_DIR="$STATE_ROOT/tester-summaries"

mkdir -p "$LOG_DIR" "$LOCK_ROOT" "$TESTER_STATE_DIR" "$TESTER_DRAFTS_ROOT" \
         "$SUMMARIES_DIR" "$(dirname "$PROJECT_DIR")"

# Per-repo lock (sibling of fixer's $STATE_ROOT/.fixer-locks/ — the two
# subsystems hold independent locks so they never block each other).
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date -Iseconds)] tester lock held for $REPO; aborting" >> "$LOG_FILE"
  exit 0
fi
echo "$BASHPID $(date -Iseconds)" > "$LOCK_DIR/owner"

# Cleanup on every exit path: drop the lock and kill any watcher
# subshells we spawned so they don't leak into the pod.
SENTINEL_WATCHER_PID=""
LIFETIME_WATCHER_PID=""
cleanup() {
  [ -n "$SENTINEL_WATCHER_PID" ] && kill "$SENTINEL_WATCHER_PID" 2>/dev/null
  [ -n "$LIFETIME_WATCHER_PID" ] && kill "$LIFETIME_WATCHER_PID" 2>/dev/null
  rm -rf "$LOCK_DIR"
}
trap cleanup EXIT

exec >> "$LOG_FILE" 2>&1

echo "============================================================"
echo "[$(date -Iseconds)] tester start  repo=$REPO"
echo "============================================================"

# ---- GH API helpers ------------------------------------------------

GH_API="https://api.github.com"
AUTH_HEADER="Authorization: Bearer $GITHUB_TOKEN"
ACCEPT_HEADER="Accept: application/vnd.github+json"
APIV_HEADER="X-GitHub-Api-Version: 2022-11-28"

fetch_main_head_sha() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/branches/main" 2>/dev/null \
  | python3 -c "import sys,json; print((json.load(sys.stdin).get('commit') or {}).get('sha') or '')" 2>/dev/null
}

HEAD_SHA="$(fetch_main_head_sha)"
if [ -z "$HEAD_SHA" ]; then
  # Some repos use a different default branch — try the API default.
  HEAD_SHA="$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO" 2>/dev/null \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
db = d.get('default_branch') or 'main'
# Re-issue: caller will fetch via separate call. Print branch name back.
print(db)
" 2>/dev/null)"
  if [ -n "$HEAD_SHA" ] && [ "$HEAD_SHA" != "main" ]; then
    DEFAULT_BRANCH="$HEAD_SHA"
    HEAD_SHA="$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
      "$GH_API/repos/$REPO/branches/$DEFAULT_BRANCH" 2>/dev/null \
      | python3 -c "import sys,json; print((json.load(sys.stdin).get('commit') or {}).get('sha') or '')" 2>/dev/null)"
  else
    DEFAULT_BRANCH="main"
  fi
else
  DEFAULT_BRANCH="main"
fi

if [ -z "$HEAD_SHA" ]; then
  echo "[tester] could not fetch HEAD for $REPO — exit"
  exit 0
fi

# Already-tested gate: bail if HEAD hasn't changed since the last run.
LAST_HEAD=""
[ -f "$LAST_HEAD_FILE" ] && LAST_HEAD="$(cat "$LAST_HEAD_FILE")"
if [ "$HEAD_SHA" = "$LAST_HEAD" ]; then
  echo "[tester] HEAD $HEAD_SHA on $DEFAULT_BRANCH already tested — exiting silently"
  exit 0
fi
echo "[tester] new HEAD: '$LAST_HEAD' → '$HEAD_SHA' on branch $DEFAULT_BRANCH"

# ---- workspace setup ----------------------------------------------

if [ ! -d "$PROJECT_DIR/.git" ]; then
  echo "[tester] cloning $REPO into $PROJECT_DIR (shallow)"
  if ! git clone --quiet --depth 50 "https://github.com/$REPO.git" "$PROJECT_DIR"; then
    echo "[tester] clone failed — exit"
    exit 0
  fi
fi
cd "$PROJECT_DIR"
git fetch --quiet origin "$DEFAULT_BRANCH" --depth 50 2>/dev/null || git fetch --quiet origin "$DEFAULT_BRANCH"
git checkout --quiet --force "$DEFAULT_BRANCH" 2>/dev/null || git checkout --quiet -b "$DEFAULT_BRANCH" "origin/$DEFAULT_BRANCH"
git reset --hard --quiet "origin/$DEFAULT_BRANCH"
git clean -fdx --quiet

# Per-tester drafts dir for THIS commit. Wrapper reads $DRAFTS_DIR/*.json
# after the agent exits and turns each into a GitHub issue.
DRAFTS_DIR="$TESTER_DRAFTS_ROOT/${REPO//\//__}-$HEAD_SHA"
rm -rf "$DRAFTS_DIR"
mkdir -p "$DRAFTS_DIR"

# Per-tester isolation so parallel testers across repos don't fight
# over the same browser profile or az CLI config. Browser plugin
# reads $BROWSER_PROFILE; az reads $AZURE_CONFIG_DIR; kubectl reads
# $KUBECONFIG.
export TESTER_REPO="$REPO"
export TESTER_HEAD_SHA="$HEAD_SHA"
export TESTER_DRAFTS_DIR="$DRAFTS_DIR"
export TESTER_BOT_LOGIN_VAL="$BOT_LOGIN"
export TESTER_REPO_OWNER="$REPO_OWNER"
export BROWSER_PROFILE="tester-${REPO//\//_}"
export AZURE_CONFIG_DIR="$STATE_ROOT/tester-azure/${REPO//\//__}"
mkdir -p "$AZURE_CONFIG_DIR"

SESSION_ID="tester-${REPO//\//-}-$(date +%s)"

# ---- TESTER prompt ------------------------------------------------
# Hard-quoted heredoc so no $variable expansion: every dynamic value
# is substituted by sed below. Avoids the .32/.36/.43 quote-escape
# bugs that bit the fixer-runner prompt.
read -r -d '' PROMPT <<'PROMPT_EOF' || true
You are working autonomously as the TESTER for repository __REPO__
at commit `__HEAD_SHA__` on branch `__DEFAULT_BRANCH__`.

## YOUR ROLE — read this first

You TEST deployed software. You DO NOT:
  - edit code
  - run `git add`/`commit`/`push`
  - open pull requests
  - look at existing issues or open PRs
  - try to "fix" anything you find

You DO:
  - inspect CI workflow runs via the github MCP
  - drive the browser plugin to test the deployed site
  - use `az`/`kubectl`/cloud CLIs to fetch logs for error context
  - stage issue drafts as JSON files in __DRAFTS_DIR__
  - emit a brief summary on stdout when done

The wrapper that spawned you will read __DRAFTS_DIR__/*.json AFTER
you exit and create GitHub issues from them. It substitutes
assignees per the rules below — do NOT include real logins.

## Issue draft format

One JSON file per finding, in __DRAFTS_DIR__, named like
`01-pipeline.json`, `03-unreachable.json`, `04-error-1.json`, etc.

Schema:
```json
{
  "title": "concise title (1 line)",
  "body": "markdown body with concrete details, log excerpts, screenshots referenced as `tester-screenshot:<path>`",
  "assigneeRole": "BOT" | "OWNER"
}
```

Use **BOT** when the issue is something the issue-solver in this
same pod CAN fix on the next iteration:
  - pipeline failure with a code-level root cause
  - page errors / failed network calls observable after a successful login

Use **OWNER** when the issue needs HUMAN action that the bot can't
take:
  - site unreachable (DNS, network, infra)
  - bot is explicitly denied access to the deployed site (Entra
    "access denied" page, not a 5xx) — only the human can grant
    the bot access

## PHASE 1 — pipeline check

Use the github MCP to fetch workflow runs on branch
`__DEFAULT_BRANCH__` at head_sha `__HEAD_SHA__`:
  - github__list_workflow_runs (filter by head_sha)
  - For each run with conclusion != "success":
    - github__list_workflow_jobs to find the failed job(s)
    - github__download_workflow_run_logs or
      github__get_workflow_run_usage to extract the error message

For each distinct failure, stage one draft:
  __DRAFTS_DIR__/01-pipeline-<workflow-name>.json
with:
  - title: "CI failure: <workflow> on commit __HEAD_SHA__"
  - body: which job failed, error excerpt (≤ 50 lines), and a
    one-sentence root-cause hypothesis if obvious from the log
  - assigneeRole: "BOT"

If ANY pipeline run failed → STOP after staging. The site is likely
not in a testable state.

## PHASE 2 — find deployed website URL

Search the local checkout for a deployed URL. Sources, in order:
  1. `.github/workflows/*.yml` — `--hostname`, `--name`, custom
     domain refs in deploy steps, env vars like `WEBAPP_URL`.
  2. `terraform/*.tf`, `*.tfvars`, terraform outputs.
  3. `README.md` / `README*` — "Deployment" or "Access" sections.
  4. `k8s/`, `kustomize`, `helm` — ingress annotations, host names.
  5. Cluster: `kubectl get ingress --all-namespaces` (only if the
     deploy obviously uses k8s and `kubectl` is wired).

Filter:
  - **Prefer** URLs that mention "dev" / no env at all.
  - **Ignore** URLs explicitly tagged "prod" or "test".
  - If a URL has no env tag in its name, treat it as dev.

If no URL is discoverable → exit silently. No draft. No summary
needed beyond a `[tester] no deployed URL found` log line.

## PHASE 3 — browser open + login

Use the browser plugin (use $BROWSER_PROFILE for isolation across
parallel testers) to navigate to the URL.

If the page does not load within 30 seconds (timeout, network error,
non-Entra 5xx page):
  - Take a screenshot.
  - Stage __DRAFTS_DIR__/03-unreachable.json:
    - title: "Test site unreachable on __HEAD_SHA__"
    - body: URL, HTTP status if any, the screenshot reference, a
      short description of the failure mode
    - assigneeRole: "OWNER"
  - STOP.

If the page loads and shows a Microsoft Entra login:
  - Drive the autonomous login per TOOLS-entra.md — use
    `$ENTRA_USERNAME` / `$ENTRA_PASSWORD` / `entra-totp` for MFA.
  - DO NOT ask the user to log in. DO NOT print the URL/code in
    chat. The browser plugin + entra-totp helper let you complete
    the login end-to-end with zero user interaction.

If Entra explicitly shows "AADSTS…" access-denied / consent-
required / not-assigned-to-app errors (NOT a timeout or 5xx):
  - Screenshot.
  - Stage __DRAFTS_DIR__/03-access-denied.json:
    - title: "Bot is denied access to test site on __HEAD_SHA__"
    - body: the exact Entra error code + message, what permission
      / role / app-assignment is needed
    - assigneeRole: "OWNER"
  - STOP.

## PHASE 4 — exercise the site

You're now logged in. Test the page generically:
  - Navigate around (top-level routes, menu items)
  - Try forms (fill with plausible test data, submit)
  - Click buttons / links
  - Watch the browser console for JS errors
  - Watch the network tab for 4xx / 5xx responses

For each DISTINCT error class (don't open 20 drafts for the same
console message repeating on every page):
  - Screenshot
  - For HTTP errors: try to pull the corresponding cloud-side log
    via `az monitor app-insights query` / `kubectl logs` / similar
    (use `$AZURE_CONFIG_DIR` so parallel testers don't fight over
    the same az profile)
  - Stage __DRAFTS_DIR__/04-error-<n>.json:
    - title: short, error-class summary
    - body: URL path that triggered it, console excerpt, network
      excerpt, cloud-log excerpt if available, screenshot
      reference
    - assigneeRole: "BOT"

Test in common, not deeply. The point is broad surface coverage.

## PHASE 5 — finalize

Print a brief summary on stdout:
  - HEAD tested: __HEAD_SHA__
  - Number of drafts staged in __DRAFTS_DIR__
  - One-line description of each

**Then your VERY LAST output line, on its own line with no surrounding
text, must be exactly:**

    TESTER_DONE __HEAD_SHA__

The wrapper greps for that prefix on a tail of the log, then
terminates this agent process within ~10 seconds and processes
your drafts. Without that line the wrapper has no way to know you
are finished and will hold the agent open until the per-turn
timeout, wasting most of an hour.

## Reminders

  - You have NO write access to the repo. No commits.
  - You don't see existing issues — don't look. Each tester run is
    independent.
  - DRAFTS_DIR for this run: __DRAFTS_DIR__
  - If you find yourself wanting to fix a bug — STOP. Stage the
    issue and let the issue-solver handle it.

Begin.
PROMPT_EOF

# Substitute placeholders into the prompt. Using sed -i on a temp file
# rather than bash variable interpolation so the prompt body can
# contain any characters (no escape hazard like the fixer prompt had).
PROMPT_FILE="$(mktemp -t tester-prompt.XXXXXX)"
printf '%s' "$PROMPT" > "$PROMPT_FILE"
sed -i \
  -e "s|__REPO__|$REPO|g" \
  -e "s|__HEAD_SHA__|$HEAD_SHA|g" \
  -e "s|__DEFAULT_BRANCH__|$DEFAULT_BRANCH|g" \
  -e "s|__DRAFTS_DIR__|$DRAFTS_DIR|g" \
  "$PROMPT_FILE"

echo "[tester] invoking agent (session-id=$SESSION_ID)"

# Watcher 1 — sentinel detection (the fast-exit path).
# The prompt instructs the agent to emit `TESTER_DONE <sha>` as its
# very last stdout line. openclaw's --local mode doesn't always
# produce a clean stop_reason=end_turn, so without this the agent
# can sit idle for up to AGENT_TURN_TIMEOUT after staging drafts.
# When we see the sentinel, give a grace window for any in-flight
# tool call to finish, then SIGTERM the agent process so the
# wrapper proceeds to drafts processing.
(
  # tail -F survives log rotation (we don't rotate, but it's the
  # right idiom for "watch a file that might not exist yet").
  tail -n 0 -F "$LOG_FILE" 2>/dev/null | while IFS= read -r line; do
    case "$line" in
      TESTER_DONE*)
        sleep 10
        pkill -TERM -f "openclaw agent --local --session-id $SESSION_ID" 2>/dev/null
        # break out of tail so the subshell exits
        break
        ;;
    esac
  done
) &
SENTINEL_WATCHER_PID=$!

# Watcher 2 — hard wall-clock backstop.
# If the sentinel never fires (e.g. model OOM'd, tool-error loop,
# generic refusal), enforce MAX_LIFETIME_SECONDS so the runner
# can't camp on the pod for an hour. SIGTERM the agent the same way.
(
  sleep "$MAX_LIFETIME_SECONDS"
  echo "[tester] MAX_LIFETIME ($MAX_LIFETIME_SECONDS s) reached — terminating agent" >> "$LOG_FILE"
  pkill -TERM -f "openclaw agent --local --session-id $SESSION_ID" 2>/dev/null
) &
LIFETIME_WATCHER_PID=$!

openclaw agent --local \
  --timeout "$AGENT_TURN_TIMEOUT" \
  --session-id "$SESSION_ID" \
  --message "$(cat "$PROMPT_FILE")" \
  || echo "[tester] agent exited non-zero ($?) — proceeding to drafts processing"

# Watchers will be killed by the EXIT trap; do it eagerly here too
# so they don't keep tailing the log after we move on.
kill "$SENTINEL_WATCHER_PID" 2>/dev/null
kill "$LIFETIME_WATCHER_PID" 2>/dev/null
SENTINEL_WATCHER_PID=""
LIFETIME_WATCHER_PID=""

rm -f "$PROMPT_FILE"

# ---- post-agent: create issues from drafts ------------------------

echo "[tester] processing drafts in $DRAFTS_DIR"
CREATED_ISSUES=()
DRAFT_COUNT=0
for draft in "$DRAFTS_DIR"/*.json; do
  [ -f "$draft" ] || continue
  DRAFT_COUNT=$((DRAFT_COUNT + 1))

  # Resolve assigneeRole → actual login. Map at create-time so the
  # agent never sees real GitHub logins (keeps the prompt
  # identity-agnostic per spec).
  payload="$(BOT_LOGIN_VAL="$BOT_LOGIN" OWNER_LOGIN="$REPO_OWNER" \
    python3 -c "
import sys, json, os
with open('$draft') as f:
    d = json.load(f)
role = d.pop('assigneeRole', 'OWNER')
login = os.environ['BOT_LOGIN_VAL'] if role == 'BOT' else os.environ['OWNER_LOGIN']
d['assignees'] = [login]
# Tag the issue so the user can tell tester-created issues apart from
# human-created ones at a glance in the GitHub list view.
labels = d.get('labels', [])
if 'tester' not in labels:
    labels.append('tester')
d['labels'] = labels
print(json.dumps(d))
" 2>/dev/null)"
  if [ -z "$payload" ]; then
    echo "[tester] WARN: could not parse draft $draft — skipping"
    continue
  fi
  resp="$(curl -fsSL -X POST \
    -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "$GH_API/repos/$REPO/issues" 2>/dev/null)"
  if [ -n "$resp" ]; then
    n="$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('number',''))" 2>/dev/null)"
    url="$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('html_url',''))" 2>/dev/null)"
    if [ -n "$n" ]; then
      CREATED_ISSUES+=("#$n $url")
      echo "[tester] created issue $REPO #$n"
    else
      echo "[tester] WARN: issue create returned unparseable response for $draft"
    fi
  else
    echo "[tester] WARN: failed to POST issue for $draft"
  fi
done

# ---- mark HEAD as tested + write summary --------------------------

echo "$HEAD_SHA" > "$LAST_HEAD_FILE"

SUMMARY_FILE="$SUMMARIES_DIR/${REPO//\//__}-$HEAD_SHA.md"
{
  echo "# tester: $REPO @ $HEAD_SHA"
  echo "_branch: $DEFAULT_BRANCH, $(date -Iseconds)_"
  echo
  if [ "${#CREATED_ISSUES[@]}" = "0" ]; then
    echo "✅ all tests passed, no issues created"
  else
    echo "🔍 ${#CREATED_ISSUES[@]} issue(s) created:"
    printf '  - %s\n' "${CREATED_ISSUES[@]}"
  fi
} > "$SUMMARY_FILE"
echo "[tester] summary written to $SUMMARY_FILE"
cat "$SUMMARY_FILE"

# Post the summary as a commit comment so the repo subscriber gets a
# GitHub notification. The "default telegram" notification path is
# handled by the openclaw chat bot: a separate skill watches
# $SUMMARIES_DIR and pushes new files to telegram (see SKILL.md in
# k8s/051-tester.yaml).
SUMMARY_BODY="$(cat "$SUMMARY_FILE")"
COMMIT_PAYLOAD="$(BODY="$SUMMARY_BODY" python3 -c '
import os, json
print(json.dumps({"body": os.environ["BODY"]}))
')"
curl -fsSL -X POST \
  -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
  -H 'Content-Type: application/json' \
  -d "$COMMIT_PAYLOAD" \
  "$GH_API/repos/$REPO/commits/$HEAD_SHA/comments" >/dev/null 2>&1 \
  || echo "[tester] note: could not post commit comment (continuing)"

echo "[$(date -Iseconds)] tester exit  repo=$REPO  sha=$HEAD_SHA  drafts=$DRAFT_COUNT  created=${#CREATED_ISSUES[@]}"

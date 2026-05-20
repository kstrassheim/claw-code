#!/bin/bash
# fixer-runner: backgrounded subprocess inside the openclaw container.
# Holds a per-repo lock, manages a shared git checkout under
# ~/.openclaw/projects/<repo>/, and runs `openclaw agent --local`
# in a poll loop: agent does one turn at a time, the wrapper checks
# the issue for new @-mention comments every POLL_INTERVAL, reacts
# :+1: to each, and re-invokes the agent with the comment as the
# next turn's user message (same --session-id so context persists).
#
# Strict one-PR-per-issue: on startup we look up any existing open PR
# linked to this issue (PR body contains "closes/fixes/resolves #<n>"
# OR head ref starts with "issue-<n>-"). If found we check out that
# branch and the prompt tells the agent to push commits to it, NOT
# open a new PR. Same check at every poll: if ANY linked PR is open,
# the fixer's job is done — exit.
#
# Args:
#   $1 repo full_name       (owner/name)
#   $2 issue number
#   $3 issue url            (https://github.com/owner/name/issues/N)
#   $4 issue title          (free text — used in the agent prompt)
#
# Required env:
#   GITHUB_TOKEN            — bot's PAT (already on the openclaw pod)
#
# Optional env:
#   FIXER_BOT_LOGIN         — bot's GH login. If unset, resolved from
#                             $GITHUB_TOKEN at startup via /user.
#   FIXER_POLL_INTERVAL     — seconds between comment polls (default 300)
#   FIXER_MAX_LIFETIME      — overall wall-clock cap, seconds (default 6h)
set -uo pipefail

REPO="$1"
ISSUE_NUM="$2"
ISSUE_URL="$3"
ISSUE_TITLE="$4"

# Resolve bot identity from $GITHUB_TOKEN unless explicitly pinned via
# FIXER_BOT_LOGIN. Hardcoding the login would couple the code to one
# deployment's identity — sibling deployments use different tokens
# (e.g. sephiroth-claw vs whatever this cluster's bot is).
if [ -n "${FIXER_BOT_LOGIN:-}" ]; then
  BOT_LOGIN="$FIXER_BOT_LOGIN"
else
  BOT_LOGIN="$(curl -fsSL \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    https://api.github.com/user 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('login',''))" \
    2>/dev/null)"
  if [ -z "$BOT_LOGIN" ]; then
    echo "FATAL: could not resolve bot identity from \$GITHUB_TOKEN /user — aborting" >&2
    exit 1
  fi
fi
POLL_INTERVAL="${FIXER_POLL_INTERVAL:-300}"
MAX_LIFETIME_SECONDS="${FIXER_MAX_LIFETIME:-$((6 * 3600))}"
AGENT_TURN_TIMEOUT=3500

STATE_ROOT="${HOME:-/home/node}/.openclaw"
PROJECTS_ROOT="$STATE_ROOT/projects"
PROJECT_DIR="$PROJECTS_ROOT/$REPO"
LOCK_ROOT="$STATE_ROOT/.fixer-locks"
LOCK_DIR="$LOCK_ROOT/${REPO//\//__}"
LOG_DIR="$STATE_ROOT/fixer-logs"
LOG_FILE="$LOG_DIR/${REPO//\//_}-${ISSUE_NUM}.log"
CURSOR_FILE="$PROJECT_DIR/.issue-${ISSUE_NUM}.cursor"

mkdir -p "$LOG_DIR" "$LOCK_ROOT" "$(dirname "$PROJECT_DIR")"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date -Iseconds)] lock held for $REPO; aborting fixer for #$ISSUE_NUM" >> "$LOG_FILE"
  exit 0
fi
echo "$BASHPID $(date -Iseconds) issue=$ISSUE_NUM" > "$LOCK_DIR/owner"

# WIPE_FULL_STATE toggles the per-issue cleanup in the EXIT trap. It is
# set to 1 ONLY when the issue itself is closed — at that point the
# fixer's memory of the issue (cursor + session jsonls) is finished
# business and can go. On every other exit (PR-exists, max-lifetime,
# crash, lock-collision) we keep the per-issue state on disk so the
# next cron tick can read the cursor and the pre-flight gate can decide
# cheaply whether to bother spawning an agent.
WIPE_FULL_STATE=0

wipe_issue_state() {
  rm -f "$CURSOR_FILE" 2>/dev/null
  rm -f "$PROJECT_DIR/.issue-${ISSUE_NUM}.ci-fingerprint" 2>/dev/null
  rm -f "$STATE_ROOT"/agents/main/sessions/issue-"${REPO//\//-}"-"$ISSUE_NUM"-*.jsonl 2>/dev/null
  rm -f "$STATE_ROOT"/agents/main/sessions/issue-"${REPO//\//-}"-"$ISSUE_NUM"-*.trajectory.jsonl 2>/dev/null
  rm -f "$STATE_ROOT"/agents/main/sessions/issue-"${REPO//\//-}"-"$ISSUE_NUM"-*.trajectory-path.json 2>/dev/null
  echo "[cleanup] wiped local state for $REPO#$ISSUE_NUM (cursor + ci-fingerprint + session files)"
}

on_exit() {
  if [ "$WIPE_FULL_STATE" = "1" ]; then
    wipe_issue_state
  fi
  rm -rf "$LOCK_DIR"
}
trap on_exit EXIT

rm -rf "$PROJECT_DIR/.fixer.lock" 2>/dev/null
exec >> "$LOG_FILE" 2>&1

echo "============================================================"
echo "[$(date -Iseconds)] fixer start  repo=$REPO  issue=#$ISSUE_NUM"
echo "============================================================"

# -- GH API helpers ---------------------------------------------------

GH_API="https://api.github.com"
AUTH_HEADER="Authorization: Bearer $GITHUB_TOKEN"
ACCEPT_HEADER="Accept: application/vnd.github+json"
APIV_HEADER="X-GitHub-Api-Version: 2022-11-28"

export FIXER_BOT_LOGIN_VAL="$BOT_LOGIN"
export FIXER_ISSUE_NUM="$ISSUE_NUM"

# Find all OPEN PRs in this repo whose body says they close issue #N,
# OR whose head ref starts with `issue-<n>-`. Output: JSON array of
# {number, head_ref, html_url, title}.
fetch_open_prs_for_issue() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls?state=open&per_page=100" \
  | python3 -c "
import sys, json, re, os
n = os.environ['FIXER_ISSUE_NUM']
pat = re.compile(r'\\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\\s+#' + n + r'\\b', re.IGNORECASE)
prefix = f'issue-{n}-'
out = []
for p in json.load(sys.stdin):
    body = p.get('body') or ''
    head_ref = (p.get('head') or {}).get('ref','')
    if pat.search(body) or head_ref.startswith(prefix):
        out.append({
            'number': p['number'],
            'head_ref': head_ref,
            'html_url': p['html_url'],
            'title': p['title'],
        })
print(json.dumps(out))
"
}

# All comments on the issue (used to seed the agent's context).
fetch_all_comments() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments?per_page=100"
}

# Issue body itself.
fetch_issue_body() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM" \
  | python3 -c "import sys,json; i=json.load(sys.stdin); print(i.get('body') or '')"
}

# Issue state ("open" or "closed"). Used to trigger full wipe on close.
fetch_issue_state() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('state','open'))"
}

# Repository owner login — the @-mention target for any question the
# bot needs to ask. Pinned to the repo owner (NOT the issue author) on
# purpose: later, the bot itself may create issues (e.g. from a chat
# command), and pinging the issue.user.login would mean the bot pings
# itself. The repo owner is always the right human to escalate to.
# Derived from `$REPO` (owner/name) so no API call needed.
repo_owner_login() {
  echo "${REPO%%/*}"
}

# Filter to comments newer than cursor where the bot is @-mentioned
# (case-insensitive). Skip the bot's own comments so we don't react to
# our own posts.
fetch_new_mentions() {
  local since_id="$1"
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments?per_page=100" \
  | python3 -c "
import sys, json, re, os
since = int('${since_id:-0}')
bot = os.environ['FIXER_BOT_LOGIN_VAL'].lower()
mention_re = re.compile(r'@' + re.escape(bot) + r'\b', re.IGNORECASE)
out = []
for c in json.load(sys.stdin):
    if c['id'] <= since:
        continue
    if (c.get('user') or {}).get('login', '').lower() == bot:
        continue
    body = c.get('body') or ''
    if not mention_re.search(body):
        continue
    out.append({
        'id': c['id'],
        'user': c['user']['login'],
        'body': body,
        'html_url': c.get('html_url'),
    })
print(json.dumps(out))
"
}

react_to_comment() {
  local cid="$1"
  curl -fsSL -X POST -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    -H 'Content-Type: application/json' \
    -d '{"content":"+1"}' \
    "$GH_API/repos/$REPO/issues/comments/$cid/reactions" >/dev/null 2>&1 \
    && echo "[react] thumbs-up on comment $cid" \
    || echo "[react] FAILED on comment $cid"
}

most_recent_comment_id() {
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/issues/$ISSUE_NUM/comments?per_page=100" \
  | python3 -c "import sys,json; cs=json.load(sys.stdin); print(max((c['id'] for c in cs), default=0))"
}

# CI fingerprint: a stable token for the CI state on the PR head. The
# head SHA is part of the fingerprint so a new push (even one whose CI
# settles with the exact same set of check conclusions as the previous
# commit) still wakes the agent — otherwise a "fix that didn't fix"
# looks identical to "no change" and the bot misses the chance to
# diagnose the next root cause.
#
# Format:
#   "no-checks:<sha7>"     — head exists, no checks reported yet
#   "in-progress:<sha7>"   — at least one check still running / queued
#   "settled:<sha7>:<hash>"— all checks settled; hash over (name,conclusion) pairs
#
# Pre-flight gate wakes the agent on ANY change. So:
#   - push of a fix → sha7 changes → wake
#   - last check settles → settled prefix → wake
#   - CI flaps red after a hotfix attempt → still wakes via sha
ci_fingerprint_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha=$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$pr_num" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null)
  if [ -z "$head_sha" ]; then echo "unknown"; return; fi
  local sha7="${head_sha:0:7}"
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/commits/$head_sha/check-runs?per_page=100" 2>/dev/null \
  | SHA7="$sha7" python3 -c "
import sys, json, hashlib, os
sha7 = os.environ['SHA7']
try:
    d = json.load(sys.stdin)
except Exception:
    print('unknown'); sys.exit(0)
runs = d.get('check_runs', [])
if not runs:
    print(f'no-checks:{sha7}'); sys.exit(0)
if any(r.get('status') != 'completed' for r in runs):
    print(f'in-progress:{sha7}'); sys.exit(0)
completed = sorted([(r['name'], r.get('conclusion') or 'unknown') for r in runs])
h = hashlib.sha256(repr(completed).encode()).hexdigest()[:16]
print(f'settled:{sha7}:{h}')
"
}

# Human-readable summary of CI on the PR head, included in the
# initial agent prompt so the agent can act on rule 8 (CI red → fix)
# or rule 9 (CI green + no more work → request review) without
# having to fetch first.
ci_summary_text_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha=$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$pr_num" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null)
  if [ -z "$head_sha" ]; then echo "(could not fetch CI status)"; return; fi
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/commits/$head_sha/check-runs?per_page=100" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('(unparseable check-runs response)'); sys.exit(0)
runs = d.get('check_runs', [])
if not runs:
    print('(no checks reported yet on head sha)'); sys.exit(0)
for r in sorted(runs, key=lambda x: x['name']):
    status = r.get('status','?')
    conclusion = r.get('conclusion') or '-'
    url = r.get('html_url','')
    marker = '✅' if conclusion == 'success' else ('❌' if conclusion in ('failure','cancelled','timed_out') else '⏳')
    print(f'{marker} {r[\"name\"]:35s} status={status:12s} conclusion={conclusion:10s} {url}')
"
}

# CI gate: returns "green" if every check-run on the PR's head SHA
# completed=success, "pending" if none have reported yet, "not_green"
# otherwise. The user's rule is "only request review when all pipelines
# are running [green]" — anything other than "green" disqualifies the
# PR from having a reviewer assigned.
ci_status_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha=$(curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$pr_num" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null)
  if [ -z "$head_sha" ]; then
    echo "unknown"
    return
  fi
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/commits/$head_sha/check-runs?per_page=100" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('unknown'); sys.exit(0)
runs = d.get('check_runs', [])
if not runs:
    print('pending')
elif all(r.get('status') == 'completed' and r.get('conclusion') == 'success' for r in runs):
    print('green')
else:
    print('not_green')
"
}

# List requested-reviewer logins on the PR (one per line).
fetch_pr_reviewers() {
  local pr_num="$1"
  curl -fsSL -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    "$GH_API/repos/$REPO/pulls/$pr_num/requested_reviewers" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for u in d.get('users', []):
    print(u['login'])
"
}

# Enforce the invariant: while CI is not all-green on a PR, that PR
# must have ZERO requested reviewers. If the agent added one
# prematurely (against rule 9), this wipes it. Idempotent + cheap to
# call on every tick.
enforce_no_reviewer_when_ci_red() {
  local pr_num="$1"
  local reviewers
  reviewers="$(fetch_pr_reviewers "$pr_num")"
  if [ -z "$reviewers" ]; then
    return 0
  fi
  local status
  status="$(ci_status_for_pr "$pr_num")"
  if [ "$status" = "green" ]; then
    echo "[ci-gate] PR #$pr_num CI green and reviewers=[$(echo "$reviewers" | tr '\n' ',' | sed 's/,$//')] — allowed"
    return 0
  fi
  local reviewers_json
  reviewers_json="$(echo "$reviewers" | python3 -c "
import sys, json
logins = [l.strip() for l in sys.stdin if l.strip()]
print(json.dumps({'reviewers': logins}))
")"
  curl -fsSL -X DELETE -H "$AUTH_HEADER" -H "$ACCEPT_HEADER" -H "$APIV_HEADER" \
    -H 'Content-Type: application/json' \
    -d "$reviewers_json" \
    "$GH_API/repos/$REPO/pulls/$pr_num/requested_reviewers" >/dev/null 2>&1 \
    && echo "[ci-gate] PR #$pr_num CI=$status — removed reviewer(s) [$(echo "$reviewers" | tr '\n' ',' | sed 's/,$//')] (rule 9: no review until all checks green)" \
    || echo "[ci-gate] PR #$pr_num CI=$status — FAILED to remove reviewers"
}

# -- detect existing PR + pick branch ---------------------------------

EXISTING_PRS_JSON="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
EXISTING_PR_COUNT="$(echo "$EXISTING_PRS_JSON" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"

if [ "$EXISTING_PR_COUNT" -ge 1 ]; then
  # Resume on the existing PR's branch. If multiple are open (legacy
  # mess from before this fix), pick the lowest-numbered one — that's
  # the first one the bot opened, and we'll work to merge IT rather
  # than continue the cascade.
  EXISTING_PR_NUMBER="$(echo "$EXISTING_PRS_JSON" | python3 -c "import sys,json; ps=sorted(json.load(sys.stdin), key=lambda p: p['number']); print(ps[0]['number'])")"
  EXISTING_PR_BRANCH="$(echo "$EXISTING_PRS_JSON" | python3 -c "import sys,json; ps=sorted(json.load(sys.stdin), key=lambda p: p['number']); print(ps[0]['head_ref'])")"
  EXISTING_PR_URL="$(echo "$EXISTING_PRS_JSON" | python3 -c "import sys,json; ps=sorted(json.load(sys.stdin), key=lambda p: p['number']); print(ps[0]['html_url'])")"
  BRANCH="$EXISTING_PR_BRANCH"
  echo "[pr] resuming existing PR #$EXISTING_PR_NUMBER on branch '$BRANCH' ($EXISTING_PR_URL)"
  echo "[pr] also-open (will note in prompt): $EXISTING_PRS_JSON"
else
  EXISTING_PR_NUMBER=""
  EXISTING_PR_BRANCH=""
  EXISTING_PR_URL=""
  BRANCH="issue-$ISSUE_NUM-fix"
  echo "[pr] no open PR linked to issue #$ISSUE_NUM yet; will work on fresh branch '$BRANCH'"
fi

# -- workspace setup --------------------------------------------------

if [ ! -d "$PROJECT_DIR/.git" ]; then
  echo "[clone] $REPO → $PROJECT_DIR"
  git clone --quiet "https://github.com/$REPO.git" "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
git fetch --quiet origin
DEFAULT_BRANCH="$(git remote show origin | awk '/HEAD branch/ {print $NF}')"
echo "[checkout] default-branch=$DEFAULT_BRANCH"

if [ -n "$EXISTING_PR_BRANCH" ] && git ls-remote --heads origin "$EXISTING_PR_BRANCH" | grep -q .; then
  # Resume on the existing PR's remote branch
  git checkout --quiet "$DEFAULT_BRANCH"
  git branch -D "$EXISTING_PR_BRANCH" 2>/dev/null || true
  git checkout --quiet -b "$EXISTING_PR_BRANCH" "origin/$EXISTING_PR_BRANCH"
  echo "[checkout] resumed existing branch $EXISTING_PR_BRANCH from origin"
else
  # Fresh branch off default
  git checkout --quiet "$DEFAULT_BRANCH"
  git reset --hard --quiet "origin/$DEFAULT_BRANCH"
  git clean -fdx --quiet
  git branch -D "$BRANCH" 2>/dev/null || true
  git checkout --quiet -b "$BRANCH"
  echo "[checkout] created fresh branch $BRANCH off $DEFAULT_BRANCH"
fi

# -- gather issue context for the agent -------------------------------

ISSUE_BODY="$(fetch_issue_body 2>/dev/null || echo '')"
ALL_COMMENTS_JSON="$(fetch_all_comments 2>/dev/null || echo '[]')"
# Default @-mention target = repo owner (NOT issue author). Stable
# even when the bot itself creates issues later via chat commands.
ISSUE_AUTHOR="$(repo_owner_login)"
echo "[mention-target] @-mention target = repo owner @$ISSUE_AUTHOR (NOT issue author; stable across bot-created issues)"

ISSUE_HISTORY_TEXT="$(python3 - <<'PY'
import os, sys, json
body = os.environ.get('FIXER_ISSUE_BODY', '')
print('## Issue body')
print(body.strip() if body else '(empty)')
print()
print('## Conversation history (most recent first)')
try:
    cs = json.loads(os.environ.get('FIXER_ALL_COMMENTS_JSON','[]'))
except Exception:
    cs = []
if not cs:
    print('(no comments yet)')
else:
    for c in reversed(cs):
        user = (c.get('user') or {}).get('login','?')
        ts = c.get('created_at','')
        body = (c.get('body') or '').strip()
        if len(body) > 1200:
            body = body[:1200] + '\n…[truncated]'
        print(f'--- @{user} at {ts} ---')
        print(body)
        print()
PY
)"
FIXER_ISSUE_BODY="$ISSUE_BODY" FIXER_ALL_COMMENTS_JSON="$ALL_COMMENTS_JSON" python3 -c "import os; pass" >/dev/null 2>&1
# Note: bash here-doc captures don't pass env through `python3 - <<`,
# so we re-run with the env set explicitly:
ISSUE_HISTORY_TEXT="$(FIXER_ISSUE_BODY="$ISSUE_BODY" FIXER_ALL_COMMENTS_JSON="$ALL_COMMENTS_JSON" python3 - <<'PY'
import os, json
body = os.environ.get('FIXER_ISSUE_BODY', '')
print('## Issue body')
print(body.strip() if body else '(empty)')
print()
print('## Conversation history (oldest first)')
try:
    cs = json.loads(os.environ.get('FIXER_ALL_COMMENTS_JSON','[]'))
except Exception:
    cs = []
if not cs:
    print('(no comments yet)')
else:
    for c in cs:
        user = (c.get('user') or {}).get('login','?')
        ts = c.get('created_at','')
        text = (c.get('body') or '').strip()
        if len(text) > 1200:
            text = text[:1200] + '\n…[truncated]'
        print(f'--- @{user} at {ts} ---')
        print(text)
        print()
PY
)"

# Existing-PRs section for the prompt
EXISTING_PRS_TEXT="$(FIXER_EXISTING_PRS="$EXISTING_PRS_JSON" python3 - <<'PY'
import os, json
try:
    prs = json.loads(os.environ.get('FIXER_EXISTING_PRS','[]'))
except Exception:
    prs = []
if not prs:
    print('(none — you may open a new PR when ready)')
else:
    for p in sorted(prs, key=lambda x: x['number']):
        print(f"- PR #{p['number']} ({p['html_url']}) head_ref=`{p['head_ref']}` — {p['title']}")
PY
)"

# -- session + initial turn -------------------------------------------

SESSION_ID="issue-${REPO//\//-}-${ISSUE_NUM}-$(date +%s)"

# Anchor the comment cursor at the latest existing comment so first
# poll doesn't pick up old ones.
if [ -f "$CURSOR_FILE" ]; then
  LAST_SEEN_ID="$(cat "$CURSOR_FILE")"
  echo "[cursor] resumed from $CURSOR_FILE = $LAST_SEEN_ID"
else
  LAST_SEEN_ID="$(most_recent_comment_id)"
  echo "$LAST_SEEN_ID" > "$CURSOR_FILE"
  echo "[cursor] initialised at $LAST_SEEN_ID"
fi

# -- early-exit gates -------------------------------------------------
# These run BEFORE the initial agent invocation so we don't burn an LLM
# turn just to discover "nothing to do". Every cron tick respawns this
# script for any open assigned issue; we need to be cheap when there's
# no actual new work.

# Gate 1: issue closed → wipe everything and exit. This is the only
# path that triggers WIPE_FULL_STATE (the user's "once an issue is
# finished he can wipe his local memory" — the definitive signal of
# finished is the issue being closed, typically via PR merge).
ISSUE_STATE="$(fetch_issue_state 2>/dev/null || echo open)"
if [ "$ISSUE_STATE" = "closed" ]; then
  echo "[$(date -Iseconds)] issue #$ISSUE_NUM is CLOSED — wiping state and exiting"
  WIPE_FULL_STATE=1
  exit 0
fi

# CI-gate enforcement on the existing PR (idempotent): if any check is
# red/pending/missing AND a reviewer is requested, remove the reviewer.
# Runs on every tick so a premature add-reviewer is unwound within ~5
# minutes (current cron schedule).
if [ -n "$EXISTING_PR_NUMBER" ]; then
  enforce_no_reviewer_when_ci_red "$EXISTING_PR_NUMBER"
fi

# Gate 2: PR open. Wake the agent only when there's something for it to
# do; otherwise exit cheaply.
#
# Wake triggers (any one of them):
#   - new @-mention to the bot since cursor (user input)
#   - CI fingerprint on the PR head changed since last seen (CI just
#     settled — agent must react per rule 8 if red, rule 9 if green)
# In either case, save the new state and fall through to the initial
# agent invocation. Otherwise exit silently.
WAKE_REASON=""
if [ -n "$EXISTING_PR_NUMBER" ]; then
  CI_FP_FILE="$PROJECT_DIR/.issue-${ISSUE_NUM}.ci-fingerprint"
  CURRENT_CI_FP="$(ci_fingerprint_for_pr "$EXISTING_PR_NUMBER" 2>/dev/null || echo unknown)"
  LAST_CI_FP=""
  [ -f "$CI_FP_FILE" ] && LAST_CI_FP="$(cat "$CI_FP_FILE")"

  # Fingerprint policy (token-saver): track every transition on disk
  # but only WAKE the agent on transitions into "settled:*". CI being
  # in_progress means there's nothing actionable yet — no point
  # spending a turn just to be told to wait.
  CI_CHANGED=0
  if [ -n "$CURRENT_CI_FP" ] && [ "$CURRENT_CI_FP" != "unknown" ] && [ "$CURRENT_CI_FP" != "$LAST_CI_FP" ]; then
    echo "$CURRENT_CI_FP" > "$CI_FP_FILE"
    case "$CURRENT_CI_FP" in
      settled:*)
        CI_CHANGED=1
        ;;
      *)
        echo "[preflight] PR #$EXISTING_PR_NUMBER CI fingerprint changed but state is '$CURRENT_CI_FP' (not settled) — tracking but NOT waking agent (saves LLM calls during in-progress phase)"
        ;;
    esac
  fi

  PREFLIGHT_NEW="$(fetch_new_mentions "$LAST_SEEN_ID" 2>/dev/null || echo '[]')"
  PREFLIGHT_NEW_COUNT="$(echo "$PREFLIGHT_NEW" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"

  if [ "$PREFLIGHT_NEW_COUNT" = "0" ] && [ "$CI_CHANGED" = "0" ]; then
    echo "[preflight] PR #$EXISTING_PR_NUMBER open, no new @-mentions since cursor=$LAST_SEEN_ID, CI fingerprint='$CURRENT_CI_FP' unchanged or not settled — exiting without agent invocation"
    exit 0
  fi

  if [ "$CI_CHANGED" = "1" ]; then
    echo "[preflight] PR #$EXISTING_PR_NUMBER CI settled: '$LAST_CI_FP' → '$CURRENT_CI_FP' — waking agent (rule 8 if red, rule 9 if green)"
    WAKE_REASON="ci-change"
  fi

  if [ "$PREFLIGHT_NEW_COUNT" != "0" ]; then
    echo "[preflight] PR #$EXISTING_PR_NUMBER $PREFLIGHT_NEW_COUNT new @-mention(s) since cursor — waking agent"
    if [ -n "$WAKE_REASON" ]; then
      WAKE_REASON="${WAKE_REASON}+user-mention"
    else
      WAKE_REASON="user-mention"
    fi
  fi

  # Pre-react + advance cursor so the initial prompt below sees them
  # consistently and we never re-prompt for the same comment on a
  # later tick.
  while read -r cid; do
    [ -z "$cid" ] && continue
    react_to_comment "$cid"
    if [ "$cid" -gt "$LAST_SEEN_ID" ]; then
      LAST_SEEN_ID="$cid"
      echo "$cid" > "$CURSOR_FILE"
    fi
  done < <(echo "$PREFLIGHT_NEW" | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(c['id'])
")
fi

if [ -n "$EXISTING_PR_NUMBER" ]; then
  BRANCH_INSTRUCTION="**An open PR for this issue already exists: PR #${EXISTING_PR_NUMBER} on branch \`${EXISTING_PR_BRANCH}\` (${EXISTING_PR_URL}).** You have ALREADY checked out that branch. Push any further commits to **this same branch** — do NOT create a new branch, do NOT open a new PR. If the PR needs updates, push commits to ${EXISTING_PR_BRANCH}; the PR will pick them up automatically."
else
  BRANCH_INSTRUCTION="No PR is open for this issue yet. When the work is ready, open ONE PR from branch \`${BRANCH}\` to \`${DEFAULT_BRANCH}\` with \"Closes #${ISSUE_NUM}\" in the body. Do NOT open multiple PRs for the same issue."
fi

# Current CI state on the PR, embedded in the prompt so the agent sees
# failures/successes without having to fetch first. Only meaningful
# when a PR exists.
if [ -n "$EXISTING_PR_NUMBER" ]; then
  CI_STATUS_TEXT="$(ci_summary_text_for_pr "$EXISTING_PR_NUMBER" 2>/dev/null || echo '(could not fetch)')"
else
  CI_STATUS_TEXT="(no PR yet — CI not applicable)"
fi

# Why-am-I-awake hint for the agent. The wrapper has already decided
# there's work to do; this just tells the agent why and what to do
# first.
if [ "$WAKE_REASON" = "ci-change" ]; then
  WAKE_REASON_TEXT="The wrapper woke you because **CI state changed** on the PR head. Inspect the CI summary below and act per rule 8 (red → fix on same branch) or rule 9 (all green + work done → request review)."
elif [ "$WAKE_REASON" = "user-mention" ]; then
  WAKE_REASON_TEXT="The wrapper woke you because the **user @-mentioned you** in a comment. Read their message in the conversation history below and respond / act."
elif [ "$WAKE_REASON" = "ci-change+user-mention" ]; then
  WAKE_REASON_TEXT="The wrapper woke you because **both CI state changed AND the user @-mentioned you**. Handle both."
else
  WAKE_REASON_TEXT="Initial run on this issue."
fi

INITIAL_PROMPT="You are working autonomously to fix GitHub issue $ISSUE_URL — \"$ISSUE_TITLE\".

You are in a checkout of $REPO at $(pwd) on branch $BRANCH (off $DEFAULT_BRANCH). The git author identity is \`$BOT_LOGIN\` (resolved at runtime from \$GITHUB_TOKEN). You have the github MCP server available.

## Why you're awake right now

$WAKE_REASON_TEXT

## What has already been said on the issue

$ISSUE_HISTORY_TEXT

## Currently-open PRs linked to this issue

$EXISTING_PRS_TEXT

## Current CI state on the PR head

$CI_STATUS_TEXT

## Branch policy — STRICT

$BRANCH_INSTRUCTION

## Protocol — follow this exactly

1. **Read the conversation history above.** Continue from where the
   previous turns left off. Do NOT post \"🚧 Starting work\" or
   similar if a previous status already says you are working — the
   user is reading these in a notification feed and duplicates are
   annoying.

2. **If the most recent user comment includes a directive or
   correction**, apply it. If the answer was given to a question you
   previously asked, use that answer.

3. **Work as autonomously as possible.** Read the codebase,
   implement the change, run tests if any exist, commit, push the
   branch indicated above. Do not delegate to subagents.

4. **Status comments**: post AT MOST one short status comment per
   meaningful state transition (started / blocked / pushed / done).
   Never repeat a status that's already in the history. One line.

5. **Asking the user is the LAST resort.** Before posting any
   question, exhaust your toolbox:
     - Use every relevant MCP server (github, k8s, terraform,
       aws/gcp/aliyun, debug, etc.). Read tool descriptions if
       you're unsure what's available — \`/tools\`-style listings
       are free, and the github MCP alone has ~30 actions.
     - Try the action, even if you're not 100% sure of the
       arguments. A failed call gives you an error message you
       can debug. Hesitation is more expensive than experiment.
     - If a tool you'd normally reach for isn't wired, look for
       a CLI substitute (\`gh\`, \`kubectl\`, \`terraform\`, \`az\`,
       \`aws\`, \`gcloud\`, \`aliyun\`, \`git\`). They're all on
       PATH.
     - Read the codebase, the failing CI logs, the issue's
       linked PRs, neighbouring docs. The answer is usually
       already written down somewhere.
   ONLY when you definitively know a blocker is a setting or
   permission you cannot change (e.g., a missing GitHub
   environment secret in someone else's account, a missing
   federated identity in Azure Entra, a feature flag you don't
   own) — THEN post ONE comment on the issue tagging
   \`@$ISSUE_AUTHOR\` with a specific, actionable question:
   what's blocked, what you tried, what setting you need
   changed. Then stop your turn. A wrapper polls for the
   reply; when the user answers (by tagging you @$BOT_LOGIN),
   you'll be re-invoked in the same session with their reply
   as the next user message.

6. **When you finish**: ensure there is exactly ONE open PR for
   this issue. Post a final status comment on the issue with the
   PR link. Then stop. Do not open additional PRs even if you
   think the previous one is wrong — push commits to it instead.

7. **Do NOT merge or close the PR — UNLESS the issue body
   explicitly says you may.** Default: stop at \"PR opened and CI
   green\". The user reviews and merges. The bot account
   (@$BOT_LOGIN) is NOT in the branch-protection bypass list, so
   an unauthorized merge attempt will fail anyway. Specifically:
     - Do not call \`gh pr merge\`, \`merge_pull_request\`, or any
       MCP tool that merges.
     - Do not call \`gh pr close\` or \`close_pull_request\`.
     - Do not call \`gh issue close\` or \`close_issue\`.
   **Exception** — the issue body grants explicit self-merge
   permission. Look for phrases like \"merge yourself\", \"feel
   free to merge\", \"auto-merge\", \"you may merge when CI
   passes\", or equivalent. If you find one, you MAY call
   \`merge_pull_request\` once ALL required CI checks are green.
   Be conservative: if it's ambiguous, default to \"do not merge\"
   and ask in a comment.

8. **If CI on the PR fails, fix it on the same branch.** Read the
   actual failing job logs FIRST — guessing from the workflow YAML
   alone wastes turns. Use the **github MCP** to fetch logs:
   \`github__list_workflow_runs\`, then
   \`github__get_workflow_run_usage\` /
   \`github__download_workflow_run_logs\` etc.

   **The github MCP is the ONLY authenticated path to GitHub from
   inside the agent.** Every other channel — \`gh\` CLI, bare
   \`curl\`, and \`web_fetch\` — runs without \$GITHUB_TOKEN
   (the openclaw exec tool sanitizes the token from subprocesses)
   and will fail predictably:
     - \`gh ...\` → \"please run gh auth login\"
     - \`curl -H \"Authorization: Bearer \$GITHUB_TOKEN\" ...\` →
       401 / empty (token is gone in exec)
     - \`web_fetch https://api.github.com/...\` → 403
       \"Must have admin rights to Repository\" (unauthenticated)
     - \`web_fetch https://github.com/.../actions/runs/...\` →
       404 \"Page not found\" (logged-out HTML wall)
   When the agent finds itself reaching for any of these against
   a github.com URL: STOP, use the github MCP instead. Re-trying
   the same unauthenticated path is the most common LLM-time
   waste in this codebase.

   Once you have the actual error, diagnose the root cause, push a
   fix commit to the SAME branch. Post a one-line status comment
   naming the failing job + root cause. Do NOT open a new PR, do
   NOT close the existing one, and do NOT declare the issue done
   while CI is red — wait for the next push to go green, then post
   the final status (or merge, if rule 7's exception applies).

   The wrapper's pre-flight gate already waits for CI to settle
   before waking you again on the next tick — you don't need to
   poll CI yourself inside the same turn. Make your fix, push, and
   stop. The next tick will pick up the new CI result.

9. **Reviewer assignment — STRICT, ENFORCED BY THE WRAPPER.**
   NEVER request a reviewer while ANY CI check on the PR head is
   queued, in_progress, pending, or has conclusion != success.
   The wrapper checks CI on every tick and will IMMEDIATELY
   REMOVE any reviewer you add while CI is not all-green —
   adding one early is wasted effort and annoys the user.

   When you need user input because you're blocked on a setting
   they own (Azure cred, GitHub secret, etc.) → **COMMENT on
   the issue with @<user> mention**. Do NOT request them as
   reviewer; the two channels are different. Mention-in-comment
   = \"I'm blocked, please help\". Reviewer-request = \"this is
   done, please review\".

   Only call \`request_reviewers\` / \`gh pr edit --add-reviewer\`
   when ALL of the following are true:
     (a) every check-run on the PR head has conclusion=success,
     (b) the PR is the final deliverable (no more commits planned),
     (c) rule 7's self-merge exception does NOT apply.
   If a specific reviewer is named in the issue body or a
   comment, use that; otherwise default to the repo owner.
   If rule 7's exception applies (you may self-merge), do NOT
   add a reviewer — proceed to merge once CI is green.

10. **Reactions:** do NOT add reactions yourself. The wrapper
    handles marking comments as read with :+1: after each poll.

11. **Empty-PR is a signal to ASK, not to declare done.** If you
    find yourself about to push a commit that, combined with the
    PR's prior commits, results in a **net-zero diff** vs the base
    branch (i.e. you've effectively undone your own earlier work
    in this PR), STOP. Do not push. Do not request review on an
    empty PR. That state is a strong signal that you misread the
    issue and need clarification.

    Instead, post ONE comment on the issue tagging
    \`@$ISSUE_AUTHOR\` summarising:
      - What you initially understood the task to be
      - What you discovered (e.g. \"found existing X in Y.yaml
        that already does this\")
      - The specific clarifying question (e.g. \"should I add
        a separate Z job to ci.yml, or is the existing Y.yaml
        flow what you intended?\")
    Then stop your turn. The wrapper will let you reply when
    the user answers.

    Quick self-check before pushing the final commit: run
    \`git diff --stat origin/<default-branch>...HEAD\` — if
    that is empty, you are in the empty-PR case.

12. **NEVER lower a test/quality threshold to make CI pass.** If a
    coverage check, lint rule, type-check, mutation score, flaky-
    retry budget, snapshot, or similar guardrail is failing, the
    fix is to **raise the code or the tests to meet the threshold**
    — NOT to lower the threshold so the failing measurement passes.

    Specifically forbidden when CI is red on a quality gate:
      - Reducing a coverage threshold (e.g. 80 → 70, or removing
        \`--fail-under\` / \`coverageThreshold\` entries)
      - Adding files/paths to a coverage \`omit\`/\`exclude\` list
        purely to dodge a failure
      - Downgrading lint rules from error to warning (or to off),
        adding \`// eslint-disable\`, \`# noqa\`, \`@ts-ignore\`,
        \`# type: ignore\` to silence the failing check
      - Deleting / skipping / \`xit\`-ing / \`@pytest.mark.skip\`-ing
        a failing test
      - Loosening type-check strictness (\`tsconfig.json strict\`,
        \`mypy --strict\`, etc.)
      - Updating a snapshot file just because it diverged, without
        verifying the new output is actually correct

    The correct response is one of:
      (a) Improve an existing test so it actually covers the new
          code path / catches the new bug
      (b) Add NEW tests covering the previously-uncovered lines
      (c) Refactor the production code to be more testable, then
          add tests
      (d) If the threshold itself is genuinely wrong (e.g. it was
          set arbitrarily and the team agreed to relax it), that
          is a SEPARATE conversation — @-mention the repo owner
          per rule 5 with the concrete numbers and your reasoning.
          Do NOT lower it as a side effect of fixing an unrelated
          PR.

    If your only path to green CI is lowering a threshold, that
    is a strong signal you are in the rule-5 LAST-RESORT case —
    ASK the repo owner instead of silently weakening the gate.

13. **ASK BEFORE writing code that depends on values you cannot
    derive from the repository.** If the task requires *any*
    identifier, name, secret, URL, or credential you would have
    to invent or leave as a placeholder, STOP writing code and
    @-mention \`@$ISSUE_AUTHOR\` first with the concrete list of
    unknowns.

    Common examples that trigger this rule:
      - Cloud resource identifiers: Azure subscription / tenant /
        resource group / container app / app registration; AWS
        account ID / region / ECR repo / cluster name; GCP project
        / location / service account
      - GitHub Actions secret/variable names you expect to exist
        (e.g. \`AZURE_CLIENT_ID\`, \`SLACK_WEBHOOK_URL\`,
        \`STRIPE_API_KEY\`)
      - Federated identity subjects, OAuth client IDs,
        DNS records, custom domains, webhook URLs
      - Third-party tokens / API keys (Sentry DSN, Datadog API
        key, etc.)
      - Internal references in the issue body (RFC numbers,
        Figma links, Confluence pages, ticket IDs) whose
        content the bot cannot fetch

    The self-check: if your draft code, workflow, or config
    would contain a literal \`<REPLACE-ME>\`, a non-derived
    environment-variable reference, or a documentation paragraph
    explaining what the user must set up before this works,
    that is a rule-13 ASK situation — NOT a deliverable.

    Specifically: do NOT use the PR description or a status
    comment as a substitute for asking. \`See the PR description
    for the list of required secrets\` is a deferred failure, not
    a question. A future CI run will fail when those secrets are
    missing and you will have wasted a turn. Ask first.

    The right shape of the ASK comment:
      @<author> Before I implement <X>, I need:
        - <unknown 1> (what is it / where do I find it?)
        - <unknown 2>
        - ...
      Or: confirm I should use defaults <D1>, <D2> and you will
      wire up <Y> after merge.

    Then stop your turn. The wrapper will let you reply when the
    user answers.

14. **If the intent is ambiguous, contradictory, or surprising,
    ASK before acting.** The bot is good at executing well-scoped
    tasks; it is bad at silently picking between equally-valid
    interpretations. When the issue body could reasonably be read
    in more than one way, or when fulfilling the literal request
    would conflict with conventions in this repo / produce a
    surprising result, do NOT pick an interpretation and proceed
    — @-mention \`@$ISSUE_AUTHOR\` with the specific ambiguity.

    Triggers that should make you stop and ask:
      - Vague action verbs with no concrete target: \"improve\",
        \"fix\", \"clean up\", \"make X better/faster/safer\",
        \"update\" — ask what specifically and what success
        looks like
      - Conflicting requirements: the request would break an
        existing test, lower an existing guarantee, contradict
        an existing pattern, or undo recent work
      - Unusual / anti-pattern-looking asks: a request that, at
        face value, looks like it would damage the codebase
        (e.g., disable a safety check, introduce a known
        anti-pattern, ship something obviously broken) — do
        NOT assume malice or stupidity; assume the user has a
        non-obvious reason and ask what it is
      - Multiple equally-defensible scopes: \"add tests\" could
        mean unit, integration, E2E, snapshot, mutation,
        property-based — ask which
      - Implicit choices you cannot derive: choice of library /
        framework / language / pattern when more than one is
        plausible
      - Anything you find yourself rationalising in your own
        chain-of-thought as \"I'll just assume they meant Y\" —
        that rationalisation is the signal you should ask instead

    The cost of asking once is one short comment + a wait for
    reply. The cost of guessing wrong is: a PR that misses the
    point, a CI cycle (or several) that does not measure what
    the user wanted, and a round of cleanup. Asking is cheaper
    when in doubt.

    **HARD ASK TRIGGERS — these override the counter-rule below.**
    When any of these patterns appears in the issue body, you must
    @-mention and ask before writing code. Do NOT rationalise. Do
    NOT resolve creatively. The pattern itself is the signal.

    Trigger A — destructive verb against a load-bearing system.
    The issue says \"remove\", \"delete\", \"disable\", \"drop\",
    \"strip\", \"turn off\", or \"get rid of\" + one of:
      - tests / test suite / test files / snapshots
      - lint config / eslint rules / prettier / formatter
      - type-checking / tsconfig strict modes / mypy strict
      - CI jobs / GitHub Actions workflows / build steps
      - coverage thresholds (also covered by rule 12)
      - monitoring / logging / error tracking / telemetry
      - security checks (CodeQL, deps audit, secret scanning)
      - authentication / authorization / access controls
      - backups / migrations / rollback paths / safety nets
    ASK what specifically goes wrong if the gate stays. The user
    may have a real reason — document it BEFORE executing. Do not
    accept \"they slow us down\" or \"we don't need them anymore\"
    at face value; ask for the concrete reason.

    Trigger B — literal conflicting verb pair on the same target.
    The issue body contains two operations that directly oppose
    each other on the same noun. Examples:
      - \"Add a feature flag for X\" + \"Remove X\"
        (the flag becomes useless after removal)
      - \"Migrate from X to Y\" + \"Keep X working\"
      - \"Disable X\" + \"Use X for Z\"
      - \"Add X\" and later in the same body \"Remove X\"
    Do NOT resolve creatively (e.g. by making one side a no-op
    fallback). ASK which phase the user wants now — both halves
    are valid in isolation; the user picked the conjunction for
    a reason.

    Trigger C — justification reads like a soft-rationale for
    a destructive change. Phrases like:
      - \"They slow down the build\"
      - \"We don't need them anymore\"
      - \"It's just legacy code\"
      - \"Make it simpler\"
      - \"Clean it up\"
    ...attached to a destructive directive (rule-14 trigger A or
    rule-12 territory). These are sometimes valid; but they are
    also what someone says before regretting a deletion. ASK for
    one concrete consequence: what breaks / what improves
    measurably / who asked for this?

    Counter-rule (to keep this from getting noisy, AND ONLY when
    no hard trigger above fires): do NOT ask about details a
    competent engineer in this repo would not bother clarifying
    — file naming, internal helper names, minor refactor style,
    where to place a new file when the convention is obvious
    from neighbouring files, etc. Make those calls yourself.

Begin."

echo "[turn 1] initial agent invocation"
openclaw agent --local \
  --timeout "$AGENT_TURN_TIMEOUT" \
  --session-id "$SESSION_ID" \
  --message "$INITIAL_PROMPT" || echo "[agent] turn 1 exited non-zero ($?) — continuing into poll loop"

# -- poll loop --------------------------------------------------------

START_TIME=$(date +%s)
turn=2

while :; do
  elapsed=$(( $(date +%s) - START_TIME ))
  if [ "$elapsed" -ge "$MAX_LIFETIME_SECONDS" ]; then
    echo "[$(date -Iseconds)] max lifetime reached — exiting"
    break
  fi

  # Exit when the issue is closed (full wipe) or when any open PR
  # linked to this issue exists (preserve cursor for next tick).
  CUR_ISSUE_STATE="$(fetch_issue_state 2>/dev/null || echo open)"
  if [ "$CUR_ISSUE_STATE" = "closed" ]; then
    echo "[$(date -Iseconds)] issue #$ISSUE_NUM closed — wiping state and exiting"
    WIPE_FULL_STATE=1
    break
  fi

  CUR_PRS="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
  CUR_PR_COUNT="$(echo "$CUR_PRS" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
  if [ "$CUR_PR_COUNT" -ge 1 ]; then
    echo "[$(date -Iseconds)] open PR exists for issue #$ISSUE_NUM — exiting (cursor preserved)"
    echo "[pr] $(echo "$CUR_PRS" | python3 -c "import sys,json; nums=[p['number'] for p in json.load(sys.stdin)]; print(','.join('#%d' % n for n in nums))")"
    break
  fi

  echo "[poll] sleeping $POLL_INTERVAL s (last_seen=$LAST_SEEN_ID)"
  sleep "$POLL_INTERVAL"

  # Re-check before doing more work.
  CUR_ISSUE_STATE="$(fetch_issue_state 2>/dev/null || echo open)"
  if [ "$CUR_ISSUE_STATE" = "closed" ]; then
    echo "[$(date -Iseconds)] issue #$ISSUE_NUM closed during sleep — wiping state and exiting"
    WIPE_FULL_STATE=1
    break
  fi
  CUR_PRS="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
  CUR_PR_COUNT="$(echo "$CUR_PRS" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
  if [ "$CUR_PR_COUNT" -ge 1 ]; then
    echo "[$(date -Iseconds)] open PR appeared during sleep — exiting (cursor preserved)"
    break
  fi

  NEW_JSON="$(fetch_new_mentions "$LAST_SEEN_ID" 2>/dev/null || echo '[]')"
  if [ -z "$NEW_JSON" ] || [ "$NEW_JSON" = "[]" ]; then
    echo "[poll] no new @-mention comments"
    continue
  fi

  while read -r cid; do
    [ -z "$cid" ] && continue
    react_to_comment "$cid"
    if [ "$cid" -gt "$LAST_SEEN_ID" ]; then
      LAST_SEEN_ID="$cid"
      echo "$cid" > "$CURSOR_FILE"
    fi
  done < <(echo "$NEW_JSON" | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(c['id'])
")

  FOLLOWUP_PROMPT="New comments on issue #$ISSUE_NUM in which you are mentioned:

$(echo "$NEW_JSON" | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(f'--- @{c[\"user\"]}: ---')
    print(c['body'])
    print()
")

Apply this guidance and continue from where you left off. Push commits to the branch you have checked out (\`$BRANCH\`); do not open a new PR — if a PR is already open, push to its branch."

  echo "[turn $turn] re-invoking agent"
  openclaw agent --local \
    --timeout "$AGENT_TURN_TIMEOUT" \
    --session-id "$SESSION_ID" \
    --message "$FOLLOWUP_PROMPT" || echo "[agent] turn $turn exited non-zero ($?) — continuing"

  # Re-run the CI-gate enforcement after the agent turn — catches the
  # case where this turn called request_reviewers despite CI still red.
  POST_TURN_PRS="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
  POST_TURN_PR_NUM="$(echo "$POST_TURN_PRS" | python3 -c "import sys,json; ps=sorted(json.load(sys.stdin), key=lambda p: p['number']); print(ps[0]['number'] if ps else '')")"
  if [ -n "$POST_TURN_PR_NUM" ]; then
    enforce_no_reviewer_when_ci_red "$POST_TURN_PR_NUM"
  fi

  turn=$(( turn + 1 ))
done

echo "[$(date -Iseconds)] fixer exit  repo=$REPO  issue=#$ISSUE_NUM  turns=$(( turn - 1 ))"

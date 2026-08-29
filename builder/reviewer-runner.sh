#!/bin/bash
# reviewer-runner: autonomous PULL-REQUEST REVIEWER. Backgrounded subprocess
# inside the openclaw container, spawned by cron-reviewer-spawn for each pull
# request the bot has been asked to review, once that pull request's head
# commit has green checks. One agent-driven review per (pull request, head
# sha, description):
#
#   1. Check out the pull request's head branch into the reviewer's OWN
#      checkout (~/.openclaw/review-projects/<owner>/<name>/ — never the
#      fixer's tree, which it would corrupt mid-fix).
#   2. Prompt `openclaw agent --local` to review the diff, run the app
#      LOCALLY when it is runnable, verify the linked issue's acceptance
#      criteria, smoke-test the main flows on localhost, and read the code
#      scanning alerts raised against this head.
#   3. Verdict lands ON THE PULL REQUEST only: one comment whose first line
#      is the machine-readable marker
#        🔎 REVIEW RESULT: APPROVED (sha <head_sha>)
#        🔎 REVIEW RESULT: CHANGES REQUIRED (sha <head_sha>)
#      followed by a real GitHub review (APPROVE / REQUEST_CHANGES). The
#      issue-solver's merge gate keys on that marker.
#   4. Record what was reviewed — the head sha AND a digest of the title and
#      body — so the next tick skips this pull request until something it
#      judged actually moves.
#
# The reviewer NEVER: edits code, pushes commits, creates issues, merges,
# closes, or touches the deployed environment. It reads, it runs things
# locally, and it reports.
#
# Args:
#   $1 repo full_name      (owner/name)
#   $2 pull request number
#   $3 head ref            (from the planner; re-resolved from the API anyway)
#   $4 head sha            (re-resolved; informational)
#
# Required env:
#   GITHUB_TOKEN           bot's PAT (already on the openclaw pod)
#
# Optional env:
#   REVIEWER_BOT_LOGIN     bot's GH login. If unset, resolved from /user.
#   REVIEWER_AGENT_TIMEOUT agent wall-clock cap, seconds (default 3500)
#   REVIEWER_LOCK_TTL      stale-lock cutoff, seconds (default 7200)
#
# A verdict is posted ONLY for a review that actually completed. An incomplete
# run posts NOTHING AT ALL and retries on the next tick — see the
# "deterministic verdict handling" block near the bottom for why that rule is
# absolute.
set -uo pipefail

REPO="${1:?repo full_name required (owner/name)}"
PR_NUMBER="${2:?pull request number required}"
PLANNED_REF="${3:-}"
PLANNED_SHA="${4:-}"

# Permission gate — see builder/project_allowlist.py. Ahead of the checkout and
# every API call, so a refused review costs nothing and posts nothing. Being
# asked for a review is a request; the allowed-projects list is the answer.
# Exit 2 is the CLI's "not permitted"; anything else means the list could not
# be read, which permits nothing either.
if ! PERM_REASON="$(project-allow check "$REPO" 2>&1)"; then
  echo "[permission] refusing to review $REPO — ${PERM_REASON:-project-allow unavailable}" >&2
  echo "[permission] Grant it from chat with:  projects add $REPO" >&2
  exit 0
fi

# The shared shell libraries. Installed without the .sh suffix in the image;
# the suffix is tried too so this runner also works in a tree where they have
# not been renamed yet. A library that is genuinely absent is not fatal: each
# call site below has a plain fallback, because a missing tuning knob must not
# stop a review from happening.
# Where the sourced helper libraries live. Next to this script when running
# from a checkout, /usr/local/bin in the image. Probed on a file that only
# ever ships as a library, so a directory holding just the CLIs is rejected.
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
[ -n "$LIB_DIR" ] && [ -r "$LIB_DIR/agent-models.sh" ] || LIB_DIR="/usr/local/bin"
_source_lib() {
  # ONLY EVER SOURCE THE .sh FILE.
  #
  # Each runtime knob ships as a PAIR in the same directory: `agent-limits` is
  # the CLI a human runs, `agent-limits.sh` is the library this sources. An
  # earlier version searched the bare name first and therefore found the CLI.
  #
  # Sourcing the CLI does not merely fail to define the helpers. The CLI parses
  # its (absent) subcommand, prints `unknown command`, and calls `exit` — and
  # in a SOURCED file that exit belongs to the caller, so the runner died on
  # its second line having done nothing. The symptom carried no error: the
  # spawner reported a successful spawn every tick while no run ever started,
  # no lock was taken and no log was written.
  #
  # The suffix is the whole rule. Testing the execute bit instead was tried and
  # rejected: some filesystems force 0755 on every file, and there the guard
  # refuses the real library and silently drops every knob back to its default.
  # A name is a fact about the file; a mode bit is a fact about the volume.
  for _d in "$LIB_DIR" /usr/local/bin; do
    if [ -r "$_d/$1.sh" ]; then . "$_d/$1.sh"; return 0; fi
  done
  return 1
}
_source_lib agent-limits || true
_source_lib agent-models || true
_source_lib agent-thinking || true
_source_lib agent-slot || true

# Runtime-adjustable via `agent-limits`; the reviewer is one-shot, so this cap
# IS its lifetime.
_REVIEWER_RUN_DEFAULT="${REVIEWER_AGENT_TIMEOUT:-3500}"
if command -v agent_limit >/dev/null 2>&1; then
  AGENT_TIMEOUT="$(agent_limit reviewer.run "$_REVIEWER_RUN_DEFAULT")"
  agent_limit_note reviewer.run "$_REVIEWER_RUN_DEFAULT" "$AGENT_TIMEOUT" 2>/dev/null || true
else
  AGENT_TIMEOUT="$_REVIEWER_RUN_DEFAULT"
fi

# Which model this run uses. Empty means inherit the config default, which is
# what an untouched deployment produces — see builder/agent-models.sh.
AGENT_MODEL=""
if command -v agent_model >/dev/null 2>&1; then
  AGENT_MODEL="$(agent_model reviewer)"
  agent_model_note reviewer "$AGENT_MODEL" 2>/dev/null || true
fi
# How hard this subsystem thinks. Same store-on-the-PVC pattern; empty means
# pass no --thinking and inherit the config default.
AGENT_THINKING=""
if command -v agent_thinking >/dev/null 2>&1; then
  AGENT_THINKING="$(agent_thinking reviewer)"
  agent_thinking_note reviewer "$AGENT_THINKING" 2>/dev/null || true
fi

STATE_ROOT="${HOME:-/home/node}/.openclaw"
# The reviewer's OWN checkout. Deliberately NOT $STATE_ROOT/projects/<repo>:
# that tree belongs to the fixer, which may be mid-fix with uncommitted work in
# it, and checking out someone else's branch underneath a running fixer
# destroys work that was never pushed.
PROJECT_DIR="$STATE_ROOT/review-projects/$REPO"
LOCK_ROOT="$STATE_ROOT/.reviewer-locks"
LOCK_DIR="$LOCK_ROOT/${REPO//\//__}"
LOG_DIR="$STATE_ROOT/reviewer-logs"
LOG_FILE="$LOG_DIR/${REPO//\//_}-${PR_NUMBER}.log"
REVIEWER_STATE_DIR="$STATE_ROOT/reviewer-state"
STATE_KEY="${REPO//\//__}__${PR_NUMBER}"
LAST_REVIEWED_FILE="$REVIEWER_STATE_DIR/$STATE_KEY.last-reviewed-sha"
ATTEMPTS_FILE="$REVIEWER_STATE_DIR/$STATE_KEY.attempts"
# Which head this run is reviewing, for `review-verdict` to check a
# verdict against. A file as well as an env var because the agent posts
# from inside its own exec sandbox; keyed per change request, so one
# review cannot be answered by what another left behind.
REVIEWING_SHA_FILE="$REVIEWER_STATE_DIR/$STATE_KEY.reviewing-sha"
SUMMARIES_DIR="$STATE_ROOT/reviewer-summaries"
SUMMARY_FILE="$SUMMARIES_DIR/$STATE_KEY.last-review.md"
LOCK_TTL="${REVIEWER_LOCK_TTL:-${REVIEWER_TTL_SECONDS:-7200}}"

mkdir -p "$LOG_DIR" "$LOCK_ROOT" "$REVIEWER_STATE_DIR" "$SUMMARIES_DIR" \
         "$(dirname "$PROJECT_DIR")"

# -- per-repo lock, with stale takeover --------------------------------
# One reviewer per repo: the review checkout cannot be shared, so two runners
# on one repo would fight in the same working tree.
#
# A runner killed without its EXIT trap firing (SIGKILL, pod restart, the
# lifetime cap) leaves an orphaned lock dir behind. The planner keeps
# re-spawning us — it ignores locks it can see are dead — so a plain
# mkdir-or-abort here would deadlock the repo forever. A lock is stale when it
# is older than the TTL, or its recorded owner is not a live reviewer-runner
# process in this pod. Same rules as builder/fixer-runner.sh.
lock_is_stale() {
  local age owner_pid
  age=$(( $(date +%s) - $(stat -c %Y "$LOCK_DIR" 2>/dev/null || date +%s) ))
  [ "$age" -ge "$LOCK_TTL" ] && return 0
  owner_pid="$(awk '{print $1; exit}' "$LOCK_DIR/owner" 2>/dev/null)"
  [ -z "$owner_pid" ] && return 0
  kill -0 "$owner_pid" 2>/dev/null || return 0
  # A live PID is not enough: PIDs are recycled, and the pod runs plenty of
  # other processes. It has to still be a reviewer-runner.
  grep -aq reviewer-runner "/proc/$owner_pid/cmdline" 2>/dev/null || return 0
  return 1
}
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if lock_is_stale; then
    echo "[$(date -Iseconds)] stale reviewer lock for $REPO — taking over for #$PR_NUMBER" >> "$LOG_FILE"
    rm -rf "$LOCK_DIR"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
      echo "[$(date -Iseconds)] lost the takeover race for $REPO; aborting reviewer for #$PR_NUMBER" >> "$LOG_FILE"
      exit 0
    fi
  else
    echo "[$(date -Iseconds)] reviewer lock held for $REPO (owner alive); aborting reviewer for #$PR_NUMBER" >> "$LOG_FILE"
    exit 0
  fi
fi
echo "$BASHPID $(date -Iseconds) pr=$PR_NUMBER" > "$LOCK_DIR/owner"

# -- shared concurrency slot and exit bookkeeping ----------------------
# Released on EXIT alongside the repo lock so a killed run never leaves a slot
# held — a leaked slot throttles all three subsystems.
on_exit() {
  command -v release_agent_slot >/dev/null 2>&1 && release_agent_slot
  rm -f "$REVIEWING_SHA_FILE"
  rm -rf "$LOCK_DIR"
}
trap on_exit EXIT

exec >> "$LOG_FILE" 2>&1
echo "============================================================"
echo "[$(date -Iseconds)] reviewer start  repo=$REPO  pr=#$PR_NUMBER"
echo "============================================================"

# -- the code host -----------------------------------------------------

# Everything this runner asks its host goes through `forge-cli`, the same
# implementation the planners import. No URL, no auth header and no API
# version appears below, so this reviews a change request wherever it lives.
FORGE=(forge-cli --repo "$REPO")

# What a person reading this reviewer's output should see the thing called.
# Not cosmetic: every line below goes onto a change request a human reads, and
# calling it by the other host's name is how a review reads as if it were
# written about somebody else's project.
CR_NOUN="$("${FORGE[@]}" noun 2>/dev/null || echo "change request")"
[ -n "$CR_NOUN" ] || CR_NOUN="change request"

# A change request's conversation is the ITEM's comments, not the
# line-anchored review comments — nothing here reads or writes those.
#
# The body travels through a FILE, never an argument: it is model output, and
# a review that quotes a shell snippet contains backticks and $(...).
post_pr_comment() { # $1 = body
  _bodyf="$(mktemp)"
  printf '%s' "$1" > "$_bodyf"
  # `comment-on-change-request`, NOT `comment`: the verdict belongs on the
  # change request. On GitHub the two coincide because a pull request is an
  # issue; on GitLab `comment` writes to /issues/<n>/notes, so this verdict
  # would have landed on whatever issue happened to carry the merge request's
  # iid — somebody else's work, and the solver would have waited forever for a
  # verdict it could not see.
  #
  # Through `review-verdict`, not straight to forge-cli: it refuses a verdict
  # naming a commit this run is not reviewing. The wrapper composes its own
  # header from $HEAD_SHA so it should never trip the guard — but it posts the
  # SUMMARY FILE's contents, which is model output, and the point of a guard
  # is that it does not depend on the writer being careful.
  review-verdict --repo "$REPO" --number "$PR_NUMBER" --body-file "$_bodyf"
  _rc=$?
  rm -f "$_bodyf"
  return $_rc
}

# The real review event, as opposed to a comment.
#
# At least one host refuses a review from the change's own author, and here
# the solver and the reviewer are the same account — so this failing is the
# EXPECTED case and not a problem. The machine-readable verdict is the RESULT
# comment, and the solver's merge gate keys on that, never on this.
submit_review() { # $1 = approve|request-changes  $2 = body
  _bodyf="$(mktemp)"
  printf '%s' "$2" > "$_bodyf"
  "${FORGE[@]}" submit-review --number "$PR_NUMBER" \
    --verdict "$1" --body-file "$_bodyf" >/dev/null 2>&1
  _rc=$?
  rm -f "$_bodyf"
  return $_rc
}

# Did THIS run post a verdict comment for THIS head?
#
# "This run" is the point: matching any comment for the sha meant an EARLIER
# verdict was read back as the current run's result. Once a verdict existed for
# a head, every later run re-read it, reported a verdict and exited without
# reviewing anything — so that head could never be re-reviewed, not even after
# its state file was cleared.
fetch_verdict_comment() {
  # `change-request-comments`, NOT `comments` — the same distinction
  # post_pr_comment makes on the way out, and it was missed on the way back
  # in. The verdict is POSTED to the change request; asking `comments` for it
  # reads /issues/<n>/notes on GitLab, i.e. whatever ISSUE happens to carry
  # the merge request's iid. So the wrapper never saw the agent's own verdict,
  # concluded the comment had not landed, and posted it a second time from the
  # summary file — every GitLab review ended with two verdict comments, and
  # the solver's merge gate had two things to key on. GitHub hid the bug
  # (there a PR *is* an issue, so both verbs resolve to the same thread);
  # fixer-runner.sh has always used the correct verb.
  "${FORGE[@]}" change-request-comments --number "$PR_NUMBER" 2>/dev/null \
  | BOT="$BOT_LOGIN" SHA="$HEAD_SHA" SINCE="${RUN_START_EPOCH:-0}" python3 -c "
import sys, json, os
from datetime import datetime
bot = os.environ['BOT'].lower()
sha = os.environ['SHA']
try:
    since = float(os.environ.get('SINCE') or 0)
except ValueError:
    since = 0.0

def created_epoch(s):
    try:
        return datetime.fromisoformat((s or '').strip().replace('Z', '+00:00')).timestamp()
    except Exception:
        return 0.0

try:
    cs = json.load(sys.stdin)
except Exception:
    cs = []
for c in reversed(cs if isinstance(cs, list) else []):
    if ((c.get('author') or {}).get('username','').lower()) != bot:
        continue
    if created_epoch(c.get('createdAt')) < since:
        continue   # a verdict from an earlier run — not this one's result
    body = (c.get('body') or '').strip()
    if not body.startswith('🔎 REVIEW RESULT:'):
        continue
    first = body.splitlines()[0]
    if sha and sha not in first:
        continue
    print('approved' if 'APPROVED' in first else 'changes')
    break
" 2>/dev/null
}

# -- re-resolve the pull request (state may have moved since the plan) --

if [ -n "${REVIEWER_BOT_LOGIN:-}" ]; then
  BOT_LOGIN="$REVIEWER_BOT_LOGIN"
else
  BOT_LOGIN="$("${FORGE[@]}" identity 2>/dev/null)"
fi
if [ -z "$BOT_LOGIN" ]; then
  echo "FATAL: could not resolve bot identity from \$GITHUB_TOKEN /user — aborting"
  exit 1
fi

if ! PR_JSON="$("${FORGE[@]}" change-request --number "$PR_NUMBER")"; then
  echo "FATAL: could not fetch $CR_NOUN #$PR_NUMBER"; exit 1
fi
# One line, one field per column. Empty values are emitted as `-` and turned
# back into "" below: `read` splits on whitespace, so a single empty field
# would shift every later one along by a column — and the column that shifts
# into HEAD_SHA decides what gets reviewed.
read -r PR_STATE PR_DRAFT HEAD_REF BASE_REF HEAD_SHA PR_URL PR_AUTHOR <<EOF
$(echo "$PR_JSON" | python3 -c "
import sys, json
p = json.load(sys.stdin)
def f(v):
    v = str(v)
    return v if v.strip() else '-'
print(f(p.get('state','')), (1 if p.get('draft') else 0),
      f(p.get('headRef','')), f(p.get('baseRef','')),
      f(p.get('headSha','')), f(p.get('url','')), f(p.get('author','')))
")
EOF
for _f in PR_STATE HEAD_REF BASE_REF HEAD_SHA PR_URL PR_AUTHOR; do
  [ "${!_f}" = "-" ] && printf -v "$_f" '%s' ""
done
PR_TITLE="$(echo "$PR_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('title',''))")"
PR_BODY="$(echo "$PR_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('body') or '')")"

if [ "$PR_STATE" != "open" ]; then
  echo "[gate] pull request #$PR_NUMBER is '$PR_STATE' — nothing to review"; exit 0
fi
if [ "$PR_DRAFT" = "1" ]; then
  echo "[gate] pull request #$PR_NUMBER is a draft — skipping"; exit 0
fi

# The planner gated on green checks, but the head may have moved since. Only
# green changes get reviewed; the next tick re-plans.
#
# The reduction is the forge's and nobody else's — this used to load the
# PLANNER as a module to borrow its copy of it, which is a strange thing for a
# runner to do and only made sense while the mapping lived there. One answer,
# one place, and `unknown` is still the cautious fallback: a state that could
# not be read must never be treated as green.
CHECK_STATE="$("${FORGE[@]}" checks --sha "$HEAD_SHA" 2>/dev/null || echo unknown)"
[ -n "$CHECK_STATE" ] || CHECK_STATE=unknown
case "$CHECK_STATE" in
  green|none) : ;;
  unknown)
    echo "[gate] could not read the check state for ${HEAD_SHA:0:8} — skipping, the next tick retries"
    exit 0 ;;
  *)
    echo "[gate] checks on ${HEAD_SHA:0:8} are '$CHECK_STATE' (not green) — skipping"
    exit 0 ;;
esac

# Record WHAT was reviewed: the head sha AND a digest of the prose judged with
# it. A verdict can turn entirely on the description — "this says Closes #5 but
# only fixes half of it" — and prose is answered by editing prose, not by
# pushing a commit. Keyed on the sha alone, such a verdict could never be
# cleared: nothing would look again, and the solver runs out of retries asking
# a human to intervene. See review_subject.py.
#
# Falls back to the bare sha if the module cannot be read: that is the old
# behaviour, which costs one extra review later, not a wrong verdict now.
record_reviewed() {
  if STAMP_SHA="$HEAD_SHA" STAMP_T="$PR_TITLE" STAMP_D="$PR_BODY" \
     LIB="${CLAW_LIB_DIR:-/usr/local/bin}" python3 -c '
import os, sys
sys.path.insert(0, os.environ["LIB"])
import review_subject
print(review_subject.stamp(os.environ["STAMP_SHA"], os.environ["STAMP_T"],
                           os.environ["STAMP_D"]))
' > "$LAST_REVIEWED_FILE".tmp 2>/dev/null && [ -s "$LAST_REVIEWED_FILE".tmp ]; then
    mv "$LAST_REVIEWED_FILE".tmp "$LAST_REVIEWED_FILE"
  else
    rm -f "$LAST_REVIEWED_FILE".tmp 2>/dev/null
    echo "$HEAD_SHA" > "$LAST_REVIEWED_FILE"
    echo "[gate] WARNING: could not stamp the reviewed prose — recorded the sha only"
  fi
}

LAST_REVIEWED=""
[ -f "$LAST_REVIEWED_FILE" ] && LAST_REVIEWED="$(cat "$LAST_REVIEWED_FILE" 2>/dev/null || true)"
if [ -n "$HEAD_SHA" ] && STORED="$LAST_REVIEWED" SHA="$HEAD_SHA" \
   PRT="$PR_TITLE" PRB="$PR_BODY" LIB="${CLAW_LIB_DIR:-/usr/local/bin}" python3 -c '
import os, sys
sys.path.insert(0, os.environ["LIB"])
import review_subject
sys.exit(0 if review_subject.already_reviewed(
    os.environ["STORED"], os.environ["SHA"], os.environ["PRT"],
    os.environ["PRB"]) else 1)
' 2>/dev/null; then
  echo "[gate] head ${HEAD_SHA:0:8} already reviewed, title and body unchanged — skipping"; exit 0
fi
echo "[pr] #$PR_NUMBER '$PR_TITLE' branch=$HEAD_REF → $BASE_REF head=${HEAD_SHA:0:8} author=@$PR_AUTHOR"

# Publish the head under review for `review-verdict`. Written only now, once
# the gates have passed and this run is really going to review THIS commit —
# earlier would name a head the run then declined to look at. Removed by the
# exit trap, so a verdict posted outside a run is not checked against a stale
# one; the guard passes anything it cannot prove wrong.
printf '%s' "$HEAD_SHA" > "$REVIEWING_SHA_FILE"
export REVIEW_HEAD_SHA="$HEAD_SHA"

# Attempt counter per head sha — for the LOG ONLY. There is deliberately no cap
# and no deadline: an incomplete review never produces a verdict, it just
# retries on the next tick, however long that takes.
#
# There used to be a cap after which the wrapper posted a synthetic "CHANGES
# REQUIRED — review could not complete". That is worse than saying nothing. A
# verdict comment is the solver's merge gate, so an unearned one blocks the
# merge permanently while carrying nothing the solver can act on: it wakes,
# finds no new feedback, exits, and repeats every five minutes forever. It also
# records the sha as reviewed, so the reviewer never looks at that head again.
# A provider outage would wedge the pull request for good.
#
# An unreviewed pull request is visible on GitHub on its own — no verdict
# comment, no review. It does not need the bot to invent a review to say so.
ATTEMPTS=0
if [ -f "$ATTEMPTS_FILE" ]; then
  read -r ATT_SHA ATT_N < "$ATTEMPTS_FILE" || true
  [ "${ATT_SHA:-}" = "$HEAD_SHA" ] && ATTEMPTS="${ATT_N:-0}"
fi
case "$ATTEMPTS" in ''|*[!0-9]*) ATTEMPTS=0 ;; esac

# True when THIS run's agent output shows a provider/infrastructure error
# rather than the agent actually reviewing and producing nothing. Scans only
# the lines this run appended to $LOG_FILE.
agent_run_hit_infra_error() {
  [ -f "$LOG_FILE" ] || return 1
  tail -n +"$(( ${_log_mark:-0} + 1 ))" "$LOG_FILE" 2>/dev/null | grep -qiE \
    'usage limit reached|rate_limit_error|insufficient (credits|balance)|quota exceeded|FailoverError|provider (unavailable|error)|ECONNRESET|ETIMEDOUT|socket hang up|502 Bad Gateway|503 Service|overloaded_error'
}

# -- linked issue (for the acceptance-criteria check) ------------------
# Branch "issue-<n>-..." / "<n>-...", else closes/fixes/resolves #n in the
# body — the same conventions the fixer uses when it opens the pull request.
LINKED_ISSUE="$(SRC="$HEAD_REF" BODY="$PR_BODY" python3 -c "
import os, re
src = os.environ.get('SRC','')
m = re.match(r'^(?:issue-)?(\d+)-', src)
if m:
    print(m.group(1)); raise SystemExit
m = re.search(r'\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b',
              os.environ.get('BODY',''), re.IGNORECASE)
print(m.group(1) if m else '')
")"
ISSUE_SECTION="(no linked issue found — review the pull request on its own terms and say so in the verdict)"
ISSUE_JSON=""
if [ -n "$LINKED_ISSUE" ]; then
  ISSUE_JSON="$("${FORGE[@]}" issue --number "$LINKED_ISSUE" 2>/dev/null || echo '{}')"
  ISSUE_COMMENTS="$("${FORGE[@]}" comments --number "$LINKED_ISSUE" 2>/dev/null || echo '[]')"
  ISSUE_SECTION="$(ISS="$ISSUE_JSON" COMMENTS="$ISSUE_COMMENTS" NUM="$LINKED_ISSUE" python3 <<'PY'
import os, json
try:
    i = json.loads(os.environ.get('ISS','{}'))
except Exception:
    i = {}
print(f"Linked issue: #{os.environ['NUM']} — {i.get('title','')} ({i.get('url','')})")
print()
print('### Issue body')
print((i.get('body') or '(empty)').strip())
print()
print('### Issue conversation (oldest first)')
try:
    cs = json.loads(os.environ.get('COMMENTS','[]'))
except Exception:
    cs = []
if not cs:
    print('(no comments)')
for c in cs if isinstance(cs, list) else []:
    user = (c.get('author') or {}).get('username','?')
    text = (c.get('body') or '').strip()
    if len(text) > 800:
        text = text[:800] + '\n…[truncated]'
    print(f"--- @{user} ---")
    print(text)
    print()
PY
)"
  echo "[issue] linked issue #$LINKED_ISSUE"
else
  echo "[issue] no linked issue"
fi

AGENT_MODEL_ARGS=()
[ -z "$AGENT_MODEL" ] || AGENT_MODEL_ARGS=(--model "$AGENT_MODEL")
[ -z "$AGENT_THINKING" ] || AGENT_MODEL_ARGS+=(--thinking "$AGENT_THINKING")

# -- diff summary ------------------------------------------------------
DIFF_SUMMARY="$("${FORGE[@]}" change-request-files --number "$PR_NUMBER" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    fs = json.load(sys.stdin)
except Exception:
    fs = []
total = 0
for f in fs if isinstance(fs, list) else []:
    path = f.get('path') or '?'
    plus, minus = f.get('added') or 0, f.get('removed') or 0
    status = f.get('status') or ''
    total += plus + minus
    print(f'- {path} (+{plus}/-{minus}' + (f' {status}' if status not in ('modified','') else '') + ')')
print(f'(total changed lines: {total})')
" 2>/dev/null || echo '(diff summary unavailable — use git diff locally)')"

# -- workspace: the reviewer's OWN checkout ----------------------------
# Cloned over HTTPS with the token in the URL, exactly as the fixer does, so a
# private repo works without an SSH key on the pod. The URL is asked of the
# forge: spelling it here meant github.com and $GITHUB_TOKEN, which on a
# GitLab deployment is the wrong host and an empty credential.
CLONE_URL="$(forge-cli --repo "$REPO" clone-url 2>/dev/null || true)"
if [ -z "$CLONE_URL" ]; then
  echo "FATAL: no clone url for $REPO — is a forge credential configured?" >&2
  exit 1
fi
if [ ! -d "$PROJECT_DIR/.git" ]; then
  echo "[clone] $REPO → $PROJECT_DIR"
  git clone --quiet "$CLONE_URL" "$PROJECT_DIR" || { echo "FATAL: clone failed"; exit 1; }
fi
cd "$PROJECT_DIR" || { echo "FATAL: cannot enter $PROJECT_DIR"; exit 1; }
git remote set-url origin "$CLONE_URL" 2>/dev/null || true
git fetch --quiet origin || { echo "FATAL: fetch failed"; exit 1; }
if ! git ls-remote --heads origin "$HEAD_REF" | grep -q .; then
  echo "FATAL: head branch '$HEAD_REF' is no longer on origin"; exit 1
fi
git checkout --quiet -f "$BASE_REF" 2>/dev/null || git checkout --quiet -f -B "$BASE_REF" "origin/$BASE_REF"
git branch -D "$HEAD_REF" 2>/dev/null || true
git checkout --quiet -b "$HEAD_REF" "origin/$HEAD_REF"
git clean -fdx --quiet 2>/dev/null || true
CHECKED_OUT_SHA="$(git rev-parse HEAD)"
echo "[checkout] $HEAD_REF @ $CHECKED_OUT_SHA (plan head ${PLANNED_SHA:0:8})"
# Review whatever is at the branch tip now; if it moved past the planned sha
# the verdict marker carries the tip we actually reviewed.
HEAD_SHA="$CHECKED_OUT_SHA"

rm -f "$SUMMARY_FILE"
SESSION_ID="review-${REPO//\//-}-${PR_NUMBER}-$(date +%s)"

# OPTIONAL per-project instructions from the target repo's root. Absent => an
# empty string => the prompt is unchanged. Read from the pull request's own
# checkout, so a project can introduce or change its instructions in the very
# pull request under review and have that apply to this review.
# Narrow facts about the repository — see the annotations section of
# project-kind.sh. Kept out of PROJECT_KINDS on purpose: nearly every
# repository here has .tf files, and a kind would fire the multi-part
# re-framing on all of them.
PROJECT_ANNOTATIONS_BLOCK=""
if command -v detect_project_annotations_from_dir >/dev/null 2>&1; then
  detect_project_annotations_from_dir "$PROJECT_DIR"
  PROJECT_ANNOTATIONS_BLOCK="$(project_annotations_block 2>/dev/null || true)"
  echo "[annotations] $REPO: ${PROJECT_ANNOTATIONS:-none}"
fi

PROJECT_INSTRUCTIONS=""
if _source_lib project-instructions; then
  PROJECT_INSTRUCTIONS="$(load_project_instructions \
    "CLAWCODE-reviewer-instructions.md" "$HEAD_SHA" "$PROJECT_DIR")"
fi
if [ -n "$PROJECT_INSTRUCTIONS" ]; then
  echo "[instructions] CLAWCODE-reviewer-instructions.md found — honouring it this review"
else
  echo "[instructions] no CLAWCODE-reviewer-instructions.md in $REPO — standard review protocol"
fi

# -- what this project actually deploys --------------------------------
# The "run the app locally" step is written for a web app. On a runbook repo, a
# warehouse or a cluster workload there is nothing to start, and the generic
# advice sends the reviewer hunting for a launch config that does not exist. A
# project can be SEVERAL of these at once and the diff may touch any of them,
# so every matching kind contributes its hint. Detected from the checkout we
# already have, with the tests shared with the solver and the deployment tester
# so all three agree on what a project is.
PROJECT_KIND_HINT=""
if _source_lib project-kind; then
  detect_project_kinds_from_dir "$PROJECT_DIR"
  echo "[project] kinds=$PROJECT_KINDS"

  _KIND_BODY_automation="PowerShell runbooks, deployed by infrastructure code rather than run from a
dev server. For step 2, 'running it' means running the test suite in the
checkout and reading the runbook itself — do NOT start a real job to review a
pull request; runbooks act on live resources. A changed or added runbook that
is not registered where the deployment picks it up never deploys: that is
CHANGES REQUIRED, as is a new runbook with no test."

  _KIND_BODY_dwh="Databases, data pipelines and a warehouse, all managed as code. For step 2,
verify by reading the SQL and pipeline definitions and, at most, with
READ-ONLY queries against a dev environment — never trigger a pipeline run to
review a pull request. Check that something upstream actually populates a new
view, and that no connection string, account key or shared-access signature
appears anywhere in the diff."

  _K8S_VERIFY_BULLETS="- **Read the manifests against the workflow.** Check how the manifests are
  applied in \`.github/workflows/\`, including any placeholder substitution or
  image-tag pinning. A manifest that is valid in isolation but breaks the
  substitution fails the deploy.
- **Look at the live cluster READ-ONLY** if it helps you judge the change:
  pods, events, logs. **Never change anything in the cluster** — not to test,
  not to demonstrate. The workflow is what deploys.
- **Infrastructure code**: validate and format-check the checkout. Never
  apply, and never plan against a state you would have to unlock.

Weight your security review toward what actually bites here: secrets in the
repo or baked into an image instead of a Secret; an identity or ServiceAccount
with more rights than the workload needs; privileged or root containers; a
missing NetworkPolicy; a Service or Ingress exposing an internal port; secrets
printed into pod logs; missing resource limits; a mutable image tag where
rollback must be deterministic; and a scheduled job with no lock or no failure
path."

  _KIND_BODY_k8s="The deliverable — or part of it — is a workload in the project's own cluster.
That part is deployed by the workflow, never launched locally, so it has no
dev server and no launch config — do not spend the run looking for one.

For step 2, verify that part like this instead:
$_K8S_VERIFY_BULLETS"

  _KIND_BODY_aksbot="The deliverable is a workload in the project's own cluster, usually a bot
instance driving a scheduled job. There is no dev server and no launch config
— do not spend the run looking for one.

For step 2, verify like this instead:
$_K8S_VERIFY_BULLETS"

  _KIND_BODY_web="An ordinary web application, and the part of this repo the generic protocol
below was written for. Step 2 ('run it locally') applies to THIS part in
full: a launch config and a dev server do exist here, even though they do not
for the parts above."

  _kind_heading() {
    case "$1" in
      automation) echo "## This is an AUTOMATION project — there is no app to start" ;;
      dwh)        echo "## This is a DATA WAREHOUSE project — there is no app to start" ;;
      k8s|aksbot) echo "## This is a CLUSTER WORKLOAD project — there is no app to start" ;;
      web)        echo "## This is a web application" ;;
    esac
  }
  _kind_body() {
    case "$1" in
      automation) printf '%s' "$_KIND_BODY_automation" ;;
      dwh)        printf '%s' "$_KIND_BODY_dwh" ;;
      k8s)        printf '%s' "$_KIND_BODY_k8s" ;;
      aksbot)     printf '%s' "$_KIND_BODY_aksbot" ;;
      web)        printf '%s' "$_KIND_BODY_web" ;;
    esac
  }

  if [ "$(kind_count)" = 1 ]; then
    # Web needs no hint on its own — the generic protocol IS the web protocol.
    # It only needs saying when the project is ALSO something else, so that
    # "there is no app to start" is not read as covering the whole repo.
    has_kind web || PROJECT_KIND_HINT="$(_kind_heading "$PROJECT_KINDS")

$(_kind_body "$PROJECT_KINDS")"
  else
    PROJECT_KIND_HINT="## This project is MORE THAN ONE THING

It deploys:
$(kinds_english).
The diff you are reviewing may touch any of them — check which, because how
you verify a change differs completely per part, and \"there is no app to
start\" is true of some parts and false of others."
    for _k in $PROJECT_KINDS; do
      PROJECT_KIND_HINT="$PROJECT_KIND_HINT

### The $(kind_title "$_k") part

$(_kind_body "$_k")"
    done
  fi
fi

# -- security findings from code scanning on THIS head -----------------
#
# Deliberately scoped to refs/pull/<n>/head, not the default branch: the
# default-branch view would report problems the author never introduced and
# miss the ones they did.
#
# Empty on every failure path — code scanning not enabled, no permission,
# threshold `off`, nothing at or above the threshold. An empty section means
# the review simply does not mention security, which is the contract: the
# checks are green, and a "no findings" paragraph in every review trains the
# reader to skip the section that will one day matter.
SECURITY_SECTION="$(REPO="$REPO" PR_NUMBER="$PR_NUMBER" \
  GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
  timeout 120 python3 "${CLAW_LIB_DIR:-/usr/local/bin}/security_reports.py" 2>/dev/null || true)"
if [ -n "$SECURITY_SECTION" ]; then
  echo "[security] $(printf '%s' "$SECURITY_SECTION" | grep -c '^- \*\*') finding(s) at or above the configured threshold — included in the review prompt"
else
  echo "[security] nothing to report at the configured threshold ($(security-level 2>/dev/null | head -1 | sed 's/.*: //'))"
fi

# -- the bot's persona -------------------------------------------------
IDENTITY_MD="$(cat "$HOME/.openclaw/workspace/IDENTITY.md" 2>/dev/null || true)"
SOUL_MD="$(cat "$HOME/.openclaw/workspace/SOUL.md" 2>/dev/null || true)"

PROMPT="You are the autonomous PULL-REQUEST REVIEWER for the GitHub repository
$REPO. You have been asked to review pull request #$PR_NUMBER —
\"$PR_TITLE\" ($PR_URL), branch \`$HEAD_REF\` → \`$BASE_REF\`, head commit
\`$HEAD_SHA\`, author @$PR_AUTHOR. Every check on the head commit is green.
Your job: review this pull request rigorously and deliver a verdict ON THE
PULL REQUEST.

You are in the reviewer's own checkout of the head branch at $(pwd)
(branch \`$HEAD_REF\`, exactly the pull request head). This checkout is
YOURS — the issue-solver uses a different directory.

## Your identity & voice

The runtime mounts your persona at \`workspace/IDENTITY.md\` and your voice at
\`workspace/SOUL.md\`. They are inlined below so you have them in this turn's
context without an extra tool call. Use this voice for the review you write.
The rules below describe **what** to do; IDENTITY.md / SOUL.md describe **how
to sound**. Voice never replaces facts: a review still names files, lines and
criteria.

### workspace/IDENTITY.md
$IDENTITY_MD

### workspace/SOUL.md
$SOUL_MD

## Changed files (from the pull request diff)

$DIFF_SUMMARY

$SECURITY_SECTION

Use \`git diff origin/$BASE_REF...HEAD\` locally for the full diff.

## The linked issue this pull request claims to resolve

$ISSUE_SECTION

## Pull request description

${PR_BODY:-(empty)}

$PROJECT_KIND_HINT

$PROJECT_ANNOTATIONS_BLOCK

$PROJECT_INSTRUCTIONS
## Review protocol — do ALL applicable steps

1. **Read the diff AND the linked issue first** (\`git diff
   origin/$BASE_REF...HEAD\`). Your PRIMARY job is to decide whether this
   pull request **correctly implements what the issue asked** — not just
   whether the code is tidy and the checks pass. Extract an explicit,
   numbered list of **acceptance criteria** from the issue body (if the
   issue has no explicit criteria, derive them from what it asks for).
   You will verify each one and report PASS/FAIL against it.
2. **Run the app LOCALLY when it is runnable — websites especially.**
   **If a project section above says there is no app to start, follow THAT
   instead of this step** — it names the verification this project actually
   supports, and hunting for a dev server that does not exist wastes the run.
   Otherwise: detect how from \`.vscode/launch.json\`, \`package.json\`
   scripts, \`frontend/\` + \`backend/\` directories, or the README, and start
   the project's own mock/dev configuration so no real credentials are needed.
   Set breakpoints on the code paths this pull request changes and step
   through at least one request where you can. If the project is not runnable
   here (library, infrastructure-only, missing runtime), review it statically
   and SAY SO in the verdict.
3. **Verify EACH acceptance criterion — this is the core of the review.**
   Go through your numbered list from step 1 and confirm every criterion is
   actually satisfied by THIS change. Prefer independent functional evidence
   over trusting the author: reproduce the issue's scenario against your
   locally running app, and for a backend change exercise the endpoint
   directly. **The author wrote the unit tests, so passing tests are
   necessary but NOT sufficient** — independently confirm the behaviour. For
   each criterion record HOW you verified it: \`ran\` (you observed it in the
   running app), \`test\` (a test you read that genuinely pins that
   behaviour), or \`code\` (you could only read the code — say so, it is the
   weakest). If a criterion is unmet, or you could not verify one that
   matters and nothing else covers it, that is grounds for CHANGES REQUIRED —
   do not approve on assumption.
4. **Smoke test** — main pages load, navigation, forms, console/render
   errors, obviously broken widgets. Focus on the areas the diff touches.
5. **Security review of the changes.** Any code scanning findings listed
   above are already scoped to this head: confirm each against the diff and
   say whether this pull request introduced it. Then look for what a scanner
   misses — a credential committed and 'removed' in a later commit is still
   in the branch history and still recoverable after a merge; that is ALWAYS
   a CHANGES-REQUIRED finding, and the fix is to rewrite the branch history
   AND rotate the credential. Report only REAL, relevant findings on the
   changed code — no boilerplate, and no findings about untouched
   pre-existing code unless severe (then mention them as pre-existing, not as
   blockers for this pull request).
6. **Code review proper:** correctness, tests added for every affected layer,
   no weakened quality gates (lowered thresholds, skipped tests, disabled
   lint), matches the project's conventions.

## Verdict — HOW TO REPORT (strict)

All communication happens ON THE $CR_NOUN (comments on #$PR_NUMBER). To post
one, write the text to a file and hand the file over:

    source ~/.openclaw/.runtime-secrets.env
    cat > /tmp/verdict.md <<'EOF'
    <text>
    EOF
    review-verdict --repo $REPO --number $PR_NUMBER --body-file /tmp/verdict.md

\`review-verdict\` is \`forge-cli comment-on-change-request\` with one check in
front: it refuses a verdict whose \`(sha ...)\` names a commit other than
$HEAD_SHA, because such a verdict is about some other review. If it refuses
yours, do not work around it — re-read the diff for THIS $CR_NOUN and write
the verdict for \`$HEAD_SHA\`.

Use a FILE, not an argument: your verdict quotes code, and backticks and
\$(...) in an argument are executed by the shell before they ever reach the
$CR_NOUN.

- **Problems found** → post ONE comment that starts with EXACTLY this first
  line:
      🔎 REVIEW RESULT: CHANGES REQUIRED (sha $HEAD_SHA)
  followed by \"**Need to do changes:**\" and a numbered list of every
  finding: file/line, what is wrong, why it matters, and what to change.
  Include functional failures, security findings and code-review issues in
  the same list. Also include the **\"## Acceptance criteria\"** checklist
  (each criterion ✅/❌ with how you verified it) so the author sees exactly
  which requirement failed.
- **Everything is fine** → post ONE comment that starts with EXACTLY:
      🔎 REVIEW RESULT: APPROVED (sha $HEAD_SHA)
  The comment MUST then contain an **\"## Acceptance criteria\"** section
  listing EVERY criterion from step 1 with a tick and how you verified it,
  e.g.:
      ## Acceptance criteria
      1. ✅ GET /api/version returns {version, commit} — ran: curl on the local app returned 200 + both fields
      2. ✅ covered by test_version.py (test) and the route is wired (code)
  Then a short line each on: whether you ran the app locally (and how, or why
  not), the smoke test, and the security outcomes. An APPROVED verdict is
  only valid if every acceptance criterion is ✅.

The wrapper submits the formal GitHub review (APPROVE / REQUEST_CHANGES) for
you once it sees your RESULT comment — you do not need to call the reviews
API yourself, and if it refuses because the bot authored the pull request,
that is fine: the RESULT comment is what counts.

## Hard rules

- Do NOT commit, push, or modify the branch — you review, the author fixes.
  Leave the working tree clean.
- Do NOT merge, close, or edit the pull request (title/body/labels), do NOT
  create or close issues, and never change repository settings.
- Do NOT comment on the linked issue. Pull request comments only.
- Validate ONLY locally — never open or test the deployed environment, and
  never perform a real login against a production identity provider.
- Stop any dev servers or debug sessions you started before finishing.

## Run summary (required)

As your LAST step write a run summary to the file \`$SUMMARY_FILE\`
(markdown, ≤30 lines). The VERY FIRST LINE must be exactly one of:
    RESULT: approved
    RESULT: changes required — N finding(s)
Then: the acceptance-criteria checklist (each criterion + PASS/FAIL + how
verified: ran/test/code), whether you ran the app locally (and how), what you
exercised, each finding (or 'clean'), and the security outcomes.

Begin."

# Concurrency gate — take a slot before touching the model. Yielding here costs
# nothing: no attempt is counted and no sha is recorded, so the next tick
# retries this same head.
SLOT_NAME="reviewer #$PR_NUMBER"
if command -v acquire_agent_slot >/dev/null 2>&1; then
  if ! acquire_agent_slot; then
    echo "[slot] no agent slot free — yielding this tick (attempt NOT counted, sha unrecorded)"
    exit 0
  fi
fi

echo "[agent] invoking reviewer agent (session $SESSION_ID, attempt $((ATTEMPTS + 1)) for ${HEAD_SHA:0:8})"
# Only a verdict comment posted from here on counts as THIS run's result. Taken
# 5s in the past to absorb small clock skew between this pod and GitHub.
RUN_START_EPOCH="$(( $(date +%s) - 5 ))"
# The reviewer drives a browser (local site, debug UI) and the openclaw agent
# process routinely fails to exit after the turn is done. Run it in its own
# process group and stop the whole group once the turn-end line appears in the
# log, or at a hard deadline.
_log_mark="$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)"
setsid openclaw agent --local \
  "${AGENT_MODEL_ARGS[@]}" \
  --timeout "$AGENT_TIMEOUT" \
  --session-id "$SESSION_ID" \
  --message "$PROMPT" </dev/null &
AGENT_LEADER=$!
_stop_agent() {
  kill -TERM -"$AGENT_LEADER" 2>/dev/null || kill -TERM "$AGENT_LEADER" 2>/dev/null
  sleep 2
  kill -KILL -"$AGENT_LEADER" 2>/dev/null || kill -KILL "$AGENT_LEADER" 2>/dev/null
  pkill -KILL -P "$AGENT_LEADER" 2>/dev/null || true
}
_agent_deadline=$(( $(date +%s) + AGENT_TIMEOUT + 120 ))
while kill -0 "$AGENT_LEADER" 2>/dev/null; do
  if tail -n +"$((_log_mark + 1))" "$LOG_FILE" 2>/dev/null | grep -q "ended with stopReason="; then
    echo "[agent] turn complete (stopReason logged) — stopping agent so the verdict can post"
    sleep 3; _stop_agent; break
  fi
  if [ "$(date +%s)" -ge "$_agent_deadline" ]; then
    echo "[agent] hard timeout ($((AGENT_TIMEOUT + 120))s) — killing agent"
    _stop_agent; break
  fi
  sleep 5
done
wait "$AGENT_LEADER" 2>/dev/null || echo "[agent] reviewer turn ended (agent process group reaped)"

# Best-effort cleanup of anything the agent left running from the local run so
# the pod doesn't leak CPU/memory between reviews. Scoped to processes rooted
# in OUR checkout.
pkill -f "$PROJECT_DIR" 2>/dev/null || true

# -- deterministic verdict handling ------------------------------------
# The source of truth for the solver is a bot-authored comment starting with
# "🔎 REVIEW RESULT:" and carrying the reviewed sha. Check whether the agent
# posted one for THIS sha in THIS run; if it wrote a summary but no comment,
# post the comment from the summary; if it produced neither, count a failed
# attempt and leave the sha unrecorded so the next tick retries.
VERDICT="$(fetch_verdict_comment)"

SUMMARY_RESULT=""
if [ -s "$SUMMARY_FILE" ]; then
  SUMMARY_RESULT="$(grep -m1 '^RESULT:' "$SUMMARY_FILE" || true)"
fi

if [ -z "$VERDICT" ] && [ -n "$SUMMARY_RESULT" ]; then
  # The agent reached a conclusion but the comment didn't land — post it.
  if printf '%s' "$SUMMARY_RESULT" | grep -qi 'approved'; then
    VERDICT="approved"
    COMMENT_BODY="🔎 REVIEW RESULT: APPROVED (sha $HEAD_SHA)

$(cat "$SUMMARY_FILE")"
  else
    VERDICT="changes"
    COMMENT_BODY="🔎 REVIEW RESULT: CHANGES REQUIRED (sha $HEAD_SHA)

**Need to do changes:**

$(cat "$SUMMARY_FILE")"
  fi
  post_pr_comment "$COMMENT_BODY" \
    && echo "[verdict] wrapper posted the $VERDICT verdict from the summary file" \
    || { echo "[verdict] WARNING: failed to post the verdict comment"; VERDICT=""; }
fi

if [ -z "$VERDICT" ]; then
  # INCOMPLETE — the agent did not reach a verdict this run. Post NOTHING and
  # record NOTHING: no comment, no review, no sha. The head stays unreviewed
  # and the next tick tries again, for as long as it takes.
  #
  # This is the rule that must never be softened. A verdict is the solver's
  # merge gate, so a guessed CHANGES REQUIRED wedges the pull request
  # permanently on nothing the author can act on, and a guessed APPROVE would
  # merge code nobody read. A provider outage must cost a retry, not a verdict.
  ATTEMPTS=$((ATTEMPTS + 1))
  echo "$HEAD_SHA $ATTEMPTS" > "$ATTEMPTS_FILE"
  if agent_run_hit_infra_error; then
    echo "[verdict] INCOMPLETE — provider error (rate limit / overload / 5xx); the code was never reviewed."
  else
    echo "[verdict] INCOMPLETE — agent produced no verdict for ${HEAD_SHA:0:8}."
  fi
  echo "[verdict] attempt $ATTEMPTS on ${HEAD_SHA:0:8} — nothing posted, sha left unrecorded; the next tick retries this head."
  exit 1
fi

# The formal review, so the verdict is visible where the host shows review
# state (and, on a project that requires reviews, actually gates the merge).
if [ "$VERDICT" = "approved" ]; then
  if submit_review approve "Autonomous review: approved — see the 🔎 REVIEW RESULT comment for the acceptance-criteria checklist."; then
    echo "[review] $CR_NOUN #$PR_NUMBER approved as a formal review"
  else
    echo "[review] the review was not recorded (the bot likely authored this $CR_NOUN) — the RESULT comment is the source of truth"
  fi
else
  if submit_review request-changes "Autonomous review: changes required — see the 🔎 REVIEW RESULT comment for the findings."; then
    echo "[review] changes requested on #$PR_NUMBER as a formal review"
  else
    echo "[review] the review was not recorded (the bot likely authored this $CR_NOUN) — the RESULT comment is the source of truth"
  fi
fi

# -- record what was reviewed ------------------------------------------
record_reviewed
rm -f "$ATTEMPTS_FILE" 2>/dev/null

# Wake the issue-solver: its planner deprioritises issues with a fresh
# awaiting-review marker; the verdict is in, so drop the marker (same pod
# filesystem). The solver's next tick sees the verdict change and either merges
# (approved) or fixes (changes required).
if [ -n "$LINKED_ISSUE" ]; then
  rm -f "$STATE_ROOT/issue-markers/${REPO//\//__}-${LINKED_ISSUE}.awaiting-review" 2>/dev/null \
    && echo "[marker] cleared the awaiting-review marker for issue #$LINKED_ISSUE" || true
fi

if command -v telegram-notify >/dev/null 2>&1; then
  if [ "$VERDICT" = "approved" ]; then
    telegram-notify "🔎 Review done: $REPO#$PR_NUMBER — APPROVED (${HEAD_SHA:0:8})" || true
  else
    telegram-notify "🔎 Review done: $REPO#$PR_NUMBER — CHANGES REQUIRED (${HEAD_SHA:0:8})" || true
  fi
fi

echo "[$(date -Iseconds)] reviewer exit  repo=$REPO  pr=#$PR_NUMBER  verdict=$VERDICT  sha=$HEAD_SHA"

# -- the code host ----------------------------------------------------

# Every question this runner asks its host goes through `forge-cli`, the same
# implementation the planners import. No URL, no auth header and no API
# version appears below it, so this solver works on whichever host the issue
# lives on, and a host's quirks are fixed once rather than in each subsystem
# that trips over them.
FORGE=(forge-cli --repo "$REPO")

# What a person reading this should see a change called.
CR_NOUN="$("${FORGE[@]}" noun 2>/dev/null || echo "change request")"
[ -n "$CR_NOUN" ] || CR_NOUN="change request"

export FIXER_BOT_LOGIN_VAL="$BOT_LOGIN"
export FIXER_ISSUE_NUM="$ISSUE_NUM"

# Find all OPEN PRs in this repo whose body says they close issue #N,
# OR whose head ref starts with `issue-<n>-`. Output: JSON array of
# {number, head_ref, html_url, title}.
fetch_open_prs_for_issue() {
  # WHICH changes close this issue is the forge's question now: linking by
  # branch name or by closing keyword is the same rule on both hosts, and it
  # was written out twice — here and in the planner — with only one of the two
  # ever fixed when it was wrong.
  "${FORGE[@]}" change-requests-for-issue --number "$ISSUE_NUM" 2>/dev/null \
  | REPO="$REPO" python3 -c "
import sys, json, os, subprocess
try:
    numbers = json.load(sys.stdin)
except Exception:
    numbers = []
out = []
for n in numbers if isinstance(numbers, list) else []:
    raw = subprocess.run(['forge-cli', '--repo', os.environ['REPO'],
                          'change-request', '--number', str(n)],
                         capture_output=True, text=True)
    if raw.returncode != 0:
        continue
    try:
        cr = json.loads(raw.stdout)
    except Exception:
        continue
    out.append({'number': cr.get('number'),
                'head_ref': cr.get('headRef') or '',
                'html_url': cr.get('url') or '',
                'title': cr.get('title') or ''})
print(json.dumps(out))
"
}

# All comments on the issue (used to seed the agent's context).
fetch_all_comments() {
  "${FORGE[@]}" comments --number "$ISSUE_NUM"
}

# Issue body itself.
fetch_issue_body() {
  "${FORGE[@]}" issue --number "$ISSUE_NUM" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('body') or '')"
}

# Issue state ("open" or "closed"). Used to trigger full wipe on close.
fetch_issue_state() {
  "${FORGE[@]}" issue --number "$ISSUE_NUM" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('state') or 'open')"
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
  "${FORGE[@]}" comments --number "$ISSUE_NUM" \
  | python3 -c "
import sys, json, re, os
since = int('${since_id:-0}')
bot = os.environ['FIXER_BOT_LOGIN_VAL'].lower()
mention_re = re.compile(r'@' + re.escape(bot) + r'\b', re.IGNORECASE)
out = []
for c in json.load(sys.stdin):
    if (c.get('id') or 0) <= since:
        continue
    author = (c.get('author') or {}).get('username', '')
    if author.lower() == bot:
        continue
    body = c.get('body') or ''
    if not mention_re.search(body):
        continue
    out.append({'id': c.get('id'), 'user': author, 'body': body})
print(json.dumps(out))
"
}

react_to_comment() {
  local cid="$1"
  # The ISSUE travels with the comment id: one host addresses a comment on its
  # own and the other only through the item it belongs to, and asking for both
  # is what lets either of them answer.
  if "${FORGE[@]}" react --number "$ISSUE_NUM" --comment-id "$cid" --emoji "+1" \
       >/dev/null 2>&1; then
    echo "[react] acknowledged comment $cid"
  else
    echo "[react] FAILED on comment $cid"
  fi
}

most_recent_comment_id() {
  "${FORGE[@]}" comments --number "$ISSUE_NUM" \
  | python3 -c "import sys,json; print(max((c.get('id') or 0 for c in json.load(sys.stdin)), default=0))"
}

# The id of the bot's own ASK note, or 0. This is where the cursor belongs on
# a FIRST run of an issue the planner already asked about — see the cursor
# block below for why anchoring at the newest note swallows the answer.
ask_note_id() {
  BOT="$BOT_LOGIN" "${FORGE[@]}" comments --number "$ISSUE_NUM" 2>/dev/null \
  | python3 -c "
import json, os, sys
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import lexical_guard
print(lexical_guard.ask_note_id(json.load(sys.stdin), os.environ.get('BOT','')) or 0)
" 2>/dev/null || echo 0
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
# The head commit of a change, asked once and reused. Four helpers below need
# it and each used to fetch it for itself.
head_sha_of_pr() {
  "${FORGE[@]}" change-request --number "$1" 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('headSha') or '')" 2>/dev/null
}

# The BRANCH a change request is built on, which is what has to be deleted
# once the change request is abandoned. Read from the change request rather
# than assumed from the issue number: a human may have opened it from a branch
# named anything at all, and deleting a branch guessed from a convention is
# how the wrong branch gets deleted.
head_ref_of_pr() {
  "${FORGE[@]}" change-request --number "$1" 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('headRef') or '')" 2>/dev/null
}

ci_fingerprint_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha="$(head_sha_of_pr "$pr_num")"
  if [ -z "$head_sha" ]; then echo "unknown"; return; fi
  local sha7="${head_sha:0:7}"
  # Each check reduced to the SAME four words every other gate uses. It used
  # to hash raw conclusions, which meant this file carried its own opinion of
  # what a conclusion means — a fifth one, quietly different from the rest.
  "${FORGE[@]}" check-list --sha "$head_sha" 2>/dev/null \
  | SHA7="$sha7" python3 -c "
import sys, json, hashlib, os
sha7 = os.environ['SHA7']
try:
    runs = json.load(sys.stdin)
except Exception:
    print('unknown'); sys.exit(0)
if not runs:
    print(f'no-checks:{sha7}'); sys.exit(0)
if any(r.get('state') == 'pending' for r in runs):
    print(f'in-progress:{sha7}'); sys.exit(0)
completed = sorted((r.get('name') or '', r.get('state') or '') for r in runs)
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
  head_sha="$(head_sha_of_pr "$pr_num")"
  if [ -z "$head_sha" ]; then echo "(could not fetch CI status)"; return; fi
  "${FORGE[@]}" check-list --sha "$head_sha" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    runs = json.load(sys.stdin)
except Exception:
    print('(could not read the checks)'); sys.exit(0)
if not runs:
    print('(no checks reported yet on the head commit)'); sys.exit(0)
marks = {'green': '\u2705', 'failed': '\u274c', 'pending': '\u23f3'}
for r in sorted(runs, key=lambda x: x.get('name') or ''):
    state = r.get('state') or 'pending'
    print(f\"{marks.get(state, '\u23f3')} {(r.get('name') or '?'):35s} {state}\")
"
}

# For every failing check-run on the PR head, fetch the job log via
# the GitHub API and pull out the last ~80 lines plus any error-
# pattern matches. We inject this into the agent's initial prompt
# under a "## Failing CI excerpt" heading so the agent doesn't have
# to remember to call github__get_job_logs before reasoning — the
# evidence is already in front of it.
ci_failing_logs_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha="$(head_sha_of_pr "$pr_num")"
  [ -n "$head_sha" ] || return 0
  # WHICH job failed and WHERE its log lives is the host's business — this
  # used to dig a job id out of a URL with a regex, which is a private detail
  # of one host's web routes and has no counterpart anywhere else.
  local raw
  raw="$("${FORGE[@]}" failing-check-log --sha "$head_sha" 2>/dev/null || true)"
  [ -n "$raw" ] || return 0
  echo "### \u274c failing check"
  echo
  echo '```'
  printf '%s' "$raw" | python3 -c "
import sys, re
raw = sys.stdin.read()
# Strip ANSI and the leading timestamp each runner prefixes, for legibility.
ansi = re.compile(r'\x1b\\[[0-9;]*m')
ts = re.compile(r'^\\d{4}-\\d{2}-\\d{2}T[0-9:.]+Z\\s*')
lines = [ts.sub('', ansi.sub('', ln)).rstrip() for ln in raw.split('\n')]
patterns = re.compile(
    r'\\b(error|ERROR|FAIL\\b|\u2717|\u2718|Failing:|AssertionError|Exception|Traceback|threshold|does not meet|below|not met|coverage for|expected.*to|ENOENT|exit code [1-9])\\b',
    re.IGNORECASE,
)
hits = [ln for ln in lines if patterns.search(ln)]
tail = [ln for ln in lines if ln.strip()][-30:]
seen = set()
out = []
for ln in hits[:40] + ['---'] + tail:
    if ln in seen: continue
    seen.add(ln)
    out.append(ln[:240])
print('\n'.join(out))
" 2>/dev/null
  echo '```'
  echo
}

# CI gate: returns "green" if every check-run on the PR's head SHA
# completed=success, "pending" if none have reported yet, "not_green"
# otherwise. The user's rule is "only request review when all pipelines
# are running [green]" — anything other than "green" disqualifies the
# PR from having a reviewer assigned.
ci_status_for_pr() {
  local pr_num="$1"
  local head_sha
  head_sha="$(head_sha_of_pr "$pr_num")"
  if [ -z "$head_sha" ]; then
    echo "unknown"
    return
  fi
  # The gate's own vocabulary, from the one place check semantics are decided.
  # `none` — a commit nobody ran anything on — is NOT green here: it says
  # nothing about whether the change works, and treating it as a pass is how a
  # change reaches review with nothing having tested it.
  local state
  state="$("${FORGE[@]}" checks --sha "$head_sha" 2>/dev/null || echo unknown)"
  case "$state" in
    green)            echo "green" ;;
    pending|none|"")  echo "pending" ;;
    failed)           echo "not_green" ;;
    *)                echo "unknown" ;;
  esac
}

# List requested-reviewer logins on the PR (one per line).
fetch_pr_reviewers() {
  "${FORGE[@]}" review-requests --number "$1" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    for name in json.load(sys.stdin):
        if name:
            print(name)
except Exception:
    pass
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
  local reviewer_csv
  reviewer_csv="$(echo "$reviewers" | tr '\n' ',' | sed 's/,$//')"
  if "${FORGE[@]}" unrequest-review --number "$pr_num" \
       --reviewers "$reviewer_csv" >/dev/null 2>&1; then
    echo "[ci-gate] #$pr_num CI=$status — withdrew the review request from [$reviewer_csv] (rule 9: no review until all checks are green)"
  else
    echo "[ci-gate] #$pr_num CI=$status — FAILED to withdraw the review request"
  fi
}


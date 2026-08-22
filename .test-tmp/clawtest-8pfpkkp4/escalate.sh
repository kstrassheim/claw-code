# -- escalation ---------------------------------------------------------
# Hand the situation to a person, ONCE.
#
# Every guard below can fail the same way if it is careless: the condition
# persists, the tick runs every five minutes, and the pull request fills with
# the same paragraph until nobody reads any of it. So an escalation is keyed
# on a FINGERPRINT of the condition it is about — the same idiom as
# request_self_review's per-sha marker, and for the same reason. The condition
# changing produces a new fingerprint and a new (single) comment; the
# condition persisting produces silence.
#
# 0 = the comment was posted now. 1 = it had already been said.
escalate_once() { # $1 = marker file, $2 = fingerprint, $3 = body, $4 = log tag
  local marker="$1" fp="$2" body="$3" tag="${4:-escalation}" already=""
  [ -f "$marker" ] && already="$(cat "$marker" 2>/dev/null)"
  if [ "$already" = "$fp" ]; then
    return 1
  fi
  post_issue_comment "$body" \
    && echo "[$tag] escalated '$fp' to @$(repo_owner_login)" \
    || echo "[$tag] WARNING: could not post the escalation comment"
  # Written whether or not the post succeeded, exactly as the review request
  # is: a comment that cannot be posted now will not post better on the next
  # tick, and retrying it forever is the spam this file is built to avoid.
  printf '%s' "$fp" > "$marker"
  # The wait is now on a person, so the planner should work other issues —
  # and the issue must SHOW that on GitHub, not only in a marker file.
  park_on_hold
  return 0
}

# -- parking on a human ------------------------------------------------
# Waiting on a person has to be VISIBLE on the issue, not only in a marker
# file on a volume nobody can read.
#
# The marker is what the planner ranks on; the `On Hold` label is what a human
# sees, and it is the label they remove to hand the issue back. Setting only
# the marker parks the issue invisibly: the board still shows it in progress,
# nobody knows it is waiting on them, and the only sign is that it quietly
# stopped moving.
#
# Creating the label if the repository lacks it is deliberate — a project that
# has never been worked by the bot has no `On Hold`, and a park that silently
# fails to label is the invisible park again.
park_on_hold() {
  local labels
  touch "$AWAITING_HUMAN_MARKER" 2>/dev/null || true

  labels="$("${FORGE[@]}" issue --number "$ISSUE_NUM" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print('\\n'.join(str(n) for n in (d.get('labels') or [])))
" 2>/dev/null)"

  # Already parked: do not write again. A no-op label write still appends a
  # timeline event, and this runs every five minutes.
  if printf '%s\n' "$labels" | tr 'A-Z' 'a-z' | tr -d ' -_' | grep -qx "onhold"; then
    echo "[park] #$ISSUE_NUM is already On Hold"
    return 0
  fi

  # Define it first: at least one host will not put a label on an issue until
  # the label exists in the project, and the park would silently fail on any
  # repository the bot has not parked in before. Already-defined is success.
  "${FORGE[@]}" ensure-label --name "On Hold" --color eee600 \
    --description "Parked, waiting on a person. Only a human removes it." \
    >/dev/null 2>&1 || true

  if "${FORGE[@]}" add-labels --number "$ISSUE_NUM" --labels "On Hold" \
       >/dev/null 2>&1; then
    echo "[park] #$ISSUE_NUM parked On Hold — waiting on a person"
  else
    echo "[park] WARNING: could not add On Hold to #$ISSUE_NUM"
  fi
}

# Is the newest comment on the issue the BOT asking a human something that
# nobody has answered?
#
# This is how an agent-initiated block is detected. The wrapper's own
# escalations park themselves; a question the AGENT decided to ask does not,
# and without this the issue stays workable, holds the repository's single
# spawn slot, and starves every other issue while waiting on a person who has
# not been told they are being waited on.
bot_awaiting_human_reply() {
  local target
  target="$(repo_owner_login 2>/dev/null)"
  [ -n "$target" ] || return 1
  "${FORGE[@]}" comments --number "$ISSUE_NUM" 2>/dev/null \
  | BOT="$BOT_LOGIN" MENTION="$target" python3 -c "
import sys, json, os
bot = os.environ['BOT'].lower()
mention = os.environ['MENTION']
try:
    cs = json.load(sys.stdin)
except Exception:
    cs = []
cs = cs if isinstance(cs, list) else []
if not cs:
    sys.exit(1)
last = cs[-1]
author = ((last.get('author') or {}).get('username') or '').lower()
body = last.get('body') or ''
# The newest comment must be the BOT's and must @-mention the human. If the
# human replied after it, the newest comment is theirs and the wait is over.
sys.exit(0 if (author == bot and mention and ('@' + mention) in body) else 1)
" 2>/dev/null
}

# Park and release the repo lock when the bot is waiting on a person and there
# is no open pull request carrying the work. Returns 0 when the caller should
# yield.
yield_if_awaiting_human() {
  local open_prs cnt
  # A CLOSED issue is FINISHED, not waiting on anyone — check this FIRST. The
  # agent closes the issue itself when a human revokes it, and its closing
  # comment is then the newest bot comment, which reads exactly like an
  # unanswered question. Parking here would re-open work that was just closed.
  if [ "$(fetch_issue_state 2>/dev/null || echo open)" = "closed" ]; then
    rm -f "$AWAITING_HUMAN_MARKER" 2>/dev/null || true
    echo "[yield] #$ISSUE_NUM is CLOSED — not parking; releasing the repo lock"
    return 0
  fi
  # An open pull request means the work is in flight, not blocked on a person;
  # the review and approval gates own that path.
  open_prs="$(fetch_open_prs_for_issue 2>/dev/null || echo '[]')"
  cnt="$(printf '%s' "$open_prs" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)"
  [ "$cnt" -ge 1 ] && return 1
  if bot_awaiting_human_reply; then
    park_on_hold
    echo "[yield] awaiting a human reply — releasing the repo lock so other issues run"
    return 0
  fi
  return 1
}


# -- the merge ---------------------------------------------------------
# The wrapper merges, not the agent. Rule 7 used to hand the merge to the
# model, and a model cannot be relied on to observe a gate it can also
# rationalise its way past — which is the entire reason the review and
# sign-off gates exist. Deciding it here also means the decision is testable
# without a model call.
# Whatever the host said when it refused, for the classifier below.
MERGE_REFUSAL=""
merge_pr() { # $1 = pr number
  local out rc
  out="$("${FORGE[@]}" merge --number "$1" 2>&1)"; rc=$?
  MERGE_REFUSAL="$out"
  return $rc
}

# Is this refusal one that will never succeed on a retry?
#
# A protected branch, a missing permission or a required review the bot cannot
# satisfy are all "a person has to press this button" — retrying every five
# minutes forever is the wrong answer and, worse, a silent one. Anything else
# (a race with mergeability, a blip) is worth another tick.
merge_refusal_is_permanent() {
  printf '%s' "$MERGE_REFUSAL" | tr 'A-Z' 'a-z' | grep -qE \
    "403|405|protected branch|not authorized|not allowed|review required|required status|resource not accessible|permission|forbidden|method not allowed"
}

# Refusals of THIS commit, counted. Resets whenever the head moves, because a
# new commit is a new question.
merge_refusal_count() { # $1 = sha -> prints the new count
  local sha="$1" prev_sha="" prev_n=0 n file="${MERGE_REFUSED_FILE:-}"
  if [ -n "$file" ] && [ -f "$file" ]; then
    read -r prev_sha prev_n < "$file" 2>/dev/null || true
  fi
  case "$prev_n" in (*[!0-9]*|"") prev_n=0 ;; esac
  if [ "$prev_sha" = "$sha" ]; then n=$((prev_n + 1)); else n=1; fi
  [ -n "$file" ] && { printf '%s %s' "$sha" "$n" > "$file" 2>/dev/null || true; }
  printf '%s' "$n"
}

# The bot has everything it needs and still cannot land the change. Say so
# where the person can see it, and park — the same visible wait as every other
# "waiting on a person", rather than a warning in a pod log nobody reads.
park_merge_blocked() { # $1 = pr number, $2 = sha, $3 = why
  local pr="$1" sha="$2" why="$3" owner
  owner="$(resolve_review_target)"
  post_issue_comment "🚧 MERGE BLOCKED (sha \`${sha:0:8}\`)

${owner:+@$owner — }everything this bot controls is green: the checks pass, the autonomous review approved \`${sha:0:8}\`, and the sign-off is on record. The host refused the merge itself ($why), which is a permission only a person has.

Please **merge PR #$pr** yourself. Merging it closes this issue; if you would rather I changed something first, say so here and @-mention \`@$BOT_LOGIN\` and I will pick it back up." \
    && echo "[merge] asked a human to press the button on PR #$pr" \
    || echo "[merge] WARNING: could not post the merge-blocked notice"
  park_on_hold
}

# Does the issue body forbid the bot from merging? A pre-existing opt-out,
# kept: some issues are opened precisely so a person presses the button.
merge_forbidden_by_issue() {
  BODY="$ISSUE_BODY" python3 -c "
import os, re, sys
body = (os.environ.get('BODY') or '').lower()
patterns = (r'do\s*not\s*merge', r\"don'?t\s*merge\", r'no\s*auto-?\s*merge',
            r'leave\s*(it\s*)?for\s*review', r'manual\s*review\s*only',
            r'hold\s*for\s*approval')
sys.exit(0 if any(re.search(p, body) for p in patterns) else 1)
" 2>/dev/null
}

# 0 = merged. 1 = not merged (and the log says which gate stopped it).
maybe_merge_green_pr() { # $1 = pr number
  local pr="$1" status facts state draft head_sha
  status="$(ci_status_for_pr "$pr")"
  if [ "$status" != "green" ]; then
    echo "[merge] PR #$pr checks are '$status' — not merging"
    return 1
  fi
  read -r state draft <<< "$(pr_merge_facts "$pr")"
  if [ "${draft:-false}" = "true" ]; then
    echo "[merge] PR #$pr is a draft — not merging"
    return 1
  fi
  case "${state:-unknown}" in
    dirty)
      echo "[merge] PR #$pr conflicts with the base branch — not merging"
      return 1 ;;
    unknown)
      # GitHub computes mergeability asynchronously and answers null while it
      # is thinking. That is "ask again", not "no".
      echo "[merge] PR #$pr mergeability not computed yet — waiting for the next tick"
      return 1 ;;
  esac
  if merge_forbidden_by_issue; then
    echo "[merge] the issue body opts out of auto-merge — leaving PR #$pr for a human"
    return 1
  fi
  head_sha="$(pr_head_sha "$pr")"
  review_gate "$pr" || return 1
  # LAST, deliberately: a person is asked to sign off only once everything
  # else has already said yes.
  approval_gate "$pr" "$head_sha" || return 1
  if ! merge_pr "$pr"; then
    local refusals
    refusals="$(merge_refusal_count "$head_sha")"
    if merge_refusal_is_permanent; then
      echo "[merge] PR #$pr was refused by the host and will stay refused — parking"
      park_merge_blocked "$pr" "$head_sha" "the host would not let me merge"
    elif [ "${refusals:-1}" -ge 3 ]; then
      echo "[merge] PR #$pr refused $refusals times on the same commit — parking"
      park_merge_blocked "$pr" "$head_sha" "refused $refusals times running"
    else
      echo "[merge] WARNING: the merge of PR #$pr was refused (attempt $refusals) — retrying next tick"
    fi
    return 1
  fi
  rm -f "${MERGE_REFUSED_FILE:-}" 2>/dev/null || true
  echo "[merge] merged PR #$pr (${head_sha:0:8})"
  RUN_OUTCOME="merged"
  # The delivery is what closes the issue, and `completed` is what says it was
  # a delivery rather than a revoke.
  close_issue_as done || true
  command -v telegram-notify >/dev/null 2>&1 \
    && telegram-notify "✅ Merged $REPO#$ISSUE_NUM via PR #$pr — $ISSUE_TITLE"
  return 0
}


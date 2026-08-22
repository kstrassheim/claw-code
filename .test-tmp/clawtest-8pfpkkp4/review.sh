# -- autonomous review gate --------------------------------------------
# The pr-reviewer subsystem (k8s/052-reviewer.yaml) reviews green pull
# requests the bot is asked to review and posts a machine-readable comment
# whose FIRST LINE is
#     🔎 REVIEW RESULT: APPROVED|CHANGES REQUIRED (sha <head_sha>)
# The solver merges only after an APPROVED verdict for the CURRENT head. A
# verdict names its sha so a verdict about an older commit can never
# green-light a newer one — which is the whole failure mode a review gate
# keyed on the pull request alone would have.

REVIEWER_ENABLED_CACHE=""
reviewer_enabled() {  # 0 = reviewer active, 1 = suspended / absent / unreachable
  if [ -z "$REVIEWER_ENABLED_CACHE" ]; then
    local ns suspend
    ns="$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null || echo claw-code-local)"
    if suspend="$(kubectl -n "$ns" get cronjob pr-reviewer -o jsonpath='{.spec.suspend}' 2>/dev/null)"; then
      [ "$suspend" = "true" ] && REVIEWER_ENABLED_CACHE=0 || REVIEWER_ENABLED_CACHE=1
    else
      # Cannot tell. Fail OPEN to the documented pre-reviewer behaviour: green
      # pull requests merge. Failing closed would mean that switching the
      # reviewer off, or losing the RBAC to ask about it, silently stops every
      # merge in every repository with nothing on the issue to say why.
      REVIEWER_ENABLED_CACHE=0
    fi
  fi
  [ "$REVIEWER_ENABLED_CACHE" = "1" ]
}

# The newest reviewer verdict on the pull request → "approved <sha>" /
# "changes <sha>" / "" (no verdict at all). The sha comes from the verdict's
# own first line, not from the pull request.
pr_review_verdict() { # $1 = pr number
  "${FORGE[@]}" change-request-comments --number "$1" 2>/dev/null \
  | BOT="$BOT_LOGIN" python3 -c "
import sys, json, re, os
bot = os.environ['BOT'].lower()
try:
    cs = json.load(sys.stdin)
except Exception:
    cs = []
for c in reversed(cs if isinstance(cs, list) else []):
    if ((c.get('author') or {}).get('username','').lower()) != bot:
        continue
    body = (c.get('body') or '').strip()
    if not body.startswith('🔎 REVIEW RESULT:'):
        continue
    first = body.splitlines()[0]
    # The sha is tolerated in whatever markdown the reviewer wrapped it in.
    # The verdict line is WRITTEN BY THE AGENT from a prompt template, and a
    # model formats a commit hash as code far more often than not — the real
    # comment reads (sha \x60211f3ea...\x60), not a bare (sha 211f3ea...) —
    # and that is a literal backtick, spelled as an escape because THIS COMMENT
    # is inside the same double-quoted shell string as the code. A strict pattern
    # matched neither, so the sha parsed EMPTY, every verdict looked like it
    # belonged to some other commit, and the solver waited for a verdict it had
    # already been given. 113 ticks on one issue before anyone noticed.
    # NO BACKTICK MAY APPEAR LITERALLY IN THIS SNIPPET. It is embedded in a
    # double-quoted shell string, where a backtick opens COMMAND SUBSTITUTION
    # — bash would run the contents as a command and hand Python a mangled
    # pattern. \x60 is the same character to the regex engine and inert to the
    # shell.
    m = re.search(r'\(\s*sha[:\s]+[\x60\'\"]*([0-9a-fA-F]{7,40})[\x60\'\"]*\s*\)',
                  first, re.IGNORECASE)
    print(('approved' if 'APPROVED' in first else 'changes') + ' ' + (m.group(1) if m else ''))
    break
" 2>/dev/null
}

# Has the awaiting-review wait outlived its TTL? 0 = yes, stop believing it.
#
# The marker records that a review was ASKED for. Whether one ever ARRIVES is
# somebody else's business, so the wait needs a bound: past REVIEW_WAIT_TTL
# the marker is treated as if it were never written. Missing marker counts as
# expired — there is nothing to wait on.
review_wait_expired() {
  [ -f "$AWAITING_REVIEW_MARKER" ] || return 0
  local age mtime
  mtime="$(stat -c %Y "$AWAITING_REVIEW_MARKER" 2>/dev/null || echo 0)"
  case "$mtime" in ''|*[!0-9]*) return 0 ;; esac
  age=$(( $(date +%s) - mtime ))
  [ "$age" -ge "${REVIEW_WAIT_TTL:-7200}" ]
}

# Ask the pr-reviewer for a review of the CURRENT head: request the bot as
# reviewer (which is what the reviewer's planner keys on) and post ONE request
# comment per sha. The marker is what makes it one per sha rather than one per
# tick — a comment every five minutes is how a pull request becomes unreadable.
request_self_review() { # $1 = pr number, $2 = head sha
  local pr="$1" sha="$2" requested=""
  # NO REVIEWER IS REQUESTED, AND THAT IS NOT A FAILURE.
  #
  # GitHub refuses to add a pull request's AUTHOR as its reviewer:
  #
  #     422  Review cannot be requested from pull request author.
  #
  # This bot authors every pull request it opens, so the request can never
  # succeed. It used to be attempted anyway, and the failure logged as a
  # WARNING — which was misleading twice over. It is not a warning, it is the
  # only possible outcome; and it implied the handshake had merely failed this
  # once, when in fact no pull request the bot opens can EVER carry it as a
  # requested reviewer.
  #
  # The reviewer therefore finds this pull request by AUTHORSHIP instead (see
  # list_reviewable_prs in reviewer-tick). Nothing has to be requested at all;
  # what follows is only the human-visible note and the per-sha marker that
  # keeps it to one comment per commit rather than one per tick.
  #
  # WHY THE NOTE LANDS LATE, AND WHY THAT IS RIGHT.
  # It is posted here — inside the merge attempt — not when the pull request
  # is opened, so it appears minutes after the PR and sometimes after the
  # reviewer has already started. That reads like latency and is not.
  #
  # Reaching this line means every gate the SOLVER controls has said yes: the
  # checks are green, there is no conflict, the PR is not a draft. So the note
  # does not mean "please review", it means "the solver has finished with this
  # pull request, the pipelines passed, and it is now waiting". Its PRESENCE
  # carries all of that — a reader who sees it knows CI is green without
  # opening the checks tab, and a PR without it is not ready to look at yet. Moving it to PR creation would destroy
  # exactly that: at creation the solver may still be pushing commits, and a
  # reader could no longer tell a finished PR from one still being worked.
  #
  # The reviewer does not need it either way — it finds the work by
  # authorship. The note is for the person reading the pull request.
  [ -f "$AWAITING_REVIEW_MARKER" ] && requested="$(cat "$AWAITING_REVIEW_MARKER" 2>/dev/null)"
  if [ "$requested" = "$sha" ] && ! review_wait_expired; then
    echo "[review-gate] review of ${sha:0:8} already requested — waiting for the verdict"
    return 0
  fi
  if [ "$requested" = "$sha" ]; then
    # Same sha, but the wait has outlived REVIEW_WAIT_TTL. Nothing is coming:
    # ask again rather than wait on a promise nobody is left to keep.
    echo "[review-gate] the review of ${sha:0:8} has been pending for over ${REVIEW_WAIT_TTL}s — asking again"
  fi
  # ON THE PULL REQUEST, where its ANSWER lands. The verdict is posted to the
  # pull request and nowhere else (reviewer-runner: "Verdict lands ON THE PULL
  # REQUEST only"), so putting the request on the issue split a question from
  # its answer: the pull request showed a verdict nobody had asked for, and
  # the issue showed a request nothing ever answered. Asked twice where the
  # request was, which is the clearest evidence there is about where people
  # look for it.
  post_pr_comment "$pr" "🔎 Requested an autonomous review of \`${sha:0:8}\` — the reviewer runs the app locally, checks the acceptance criteria and scans the change. I merge only after its ✅." \
    && echo "[review-gate] posted the review request for ${sha:0:8} on PR #$pr" \
    || echo "[review-gate] WARNING: failed to post the review request"
  printf '%s' "$sha" > "$AWAITING_REVIEW_MARKER"
}

# The gate. 0 → satisfied (or the reviewer is off): the caller may merge.
# 1 → do NOT merge yet.
review_gate() { # $1 = pr number
  if ! reviewer_enabled; then
    # Reviewer suspended or unreachable → the documented pre-reviewer
    # behaviour: a green pull request merges. Drop any stale wait state so the
    # gate does not resume mid-wait when the reviewer is switched back on.
    rm -f "$AWAITING_REVIEW_MARKER" 2>/dev/null
    echo "[review-gate] the pr-reviewer CronJob is suspended — merging green pull requests directly"
    return 0
  fi
  local head_sha verdict vsha
  head_sha="$(pr_head_sha "$1")"
  [ -z "$head_sha" ] && { echo "[review-gate] could not resolve the head sha of PR #$1 — not merging"; return 1; }
  read -r verdict vsha <<< "$(pr_review_verdict "$1")"
  if [ "${verdict:-}" = "approved" ] && [ "${vsha:-}" = "$head_sha" ]; then
    rm -f "$AWAITING_REVIEW_MARKER" 2>/dev/null
    echo "[review-gate] the reviewer APPROVED ${head_sha:0:8} — merge may proceed"
    return 0
  fi
  if [ "${verdict:-}" = "changes" ] && [ "${vsha:-}" = "$head_sha" ]; then
    echo "[review-gate] the reviewer requires CHANGES on ${head_sha:0:8} — not merging"
    return 1
  fi
  # No verdict for THIS head: none at all, or one about a commit that has been
  # superseded. A push invalidates a verdict, so this is a fresh request.
  request_self_review "$1" "$head_sha"
  return 1
}


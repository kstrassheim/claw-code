# -- human sign-off gate (the `approval` label) ------------------------
# Everything above answers "may this be merged?" with machinery. This answers
# it with a person, and it is the LAST gate: it only ever runs on a pull
# request that is already green, already conflict-free, not a draft and
# already approved by the autonomous reviewer. The label means "I want to look
# at this before it lands", and the question is only worth asking once
# everything else has already said yes.
#
# It FAILS OPEN when the labels cannot be read — see story_estimate.
# requires_approval for the reasoning: a gate that can turn itself on by
# accident would stop every merge in every repository for a reason invisible
# from the issue.
APPROVAL_REQUIRED_CACHE=""
approval_required() {  # 0 = a human must sign off before the merge
  if [ -z "$APPROVAL_REQUIRED_CACHE" ]; then
    local answer
    answer="$(LABELS="$ISSUE_LABELS_JSON" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import story_estimate
print('1' if story_estimate.requires_approval(json.loads(os.environ['LABELS'] or '[]')) else '0')
" 2>/dev/null)"
    case "$answer" in
      1) APPROVAL_REQUIRED_CACHE=1 ;;
      0) APPROVAL_REQUIRED_CACHE=0 ;;
      *) APPROVAL_REQUIRED_CACHE=0
         echo "[approval-gate] could not read the labels of #$ISSUE_NUM — proceeding without a sign-off gate" ;;
    esac
  fi
  [ "$APPROVAL_REQUIRED_CACHE" = "1" ]
}

# Has a HUMAN approved this exact head? Prints the login, or nothing.
#
# GitHub's own review state, keyed on `commit_id`: an approval is given to the
# code somebody actually read, so a later push must not inherit it. The bot's
# own review never counts — it is the same account that opened the pull
# request, and an account approving itself is not a sign-off.
# Has a human signed this sha off? The rule lives in `approval_release` and
# NOT here, because the planner has to reach the same verdict about the same
# sign-off: it takes the `On Hold` label off, this decides whether the merge
# may proceed, and if they disagree the issue is released into a gate that
# re-asks and re-parks it every tick. One module, two callers.
#
# Both hosts answer through `review-verdicts`, which normalises a GitHub
# review and a GitLab approval to the same `verdict: approved` row — the
# sign-off works the same way on either, which is the point of the forge.
pr_human_approval() { # $1 = pr number, $2 = head sha
  local vfile cfile who
  vfile="$(mktemp)" || return 0
  cfile="$(mktemp)" || { rm -f "$vfile"; return 0; }
  "${FORGE[@]}" review-verdicts --number "$1" >"$vfile" 2>/dev/null \
    || printf '[]' >"$vfile"
  "${FORGE[@]}" comments --number "$ISSUE_NUM" >"$cfile" 2>/dev/null \
    || printf '[]' >"$cfile"
  who="$(BOT="$BOT_LOGIN" SHA="$2" V="$vfile" C="$cfile" python3 -c "
import sys, json, os
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import approval_release

def load(path):
    try:
        with open(path) as fh:
            rows = json.load(fh)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []

comments = load(os.environ['C'])
bot = os.environ['BOT']
ask = approval_release.approval_ask(comments, bot)
got = approval_release.signed_off(verdicts=load(os.environ['V']),
                                  comments=comments,
                                  bot=bot,
                                  sha=os.environ.get('SHA', ''),
                                  anchor_id=(ask or {}).get('id'))
print((got or {}).get('who') or '')
")"
  rm -f "$vfile" "$cfile"
  printf '%s' "$who"
}

# Has a human REFUSED this sha? Same module, same reason as above: the planner
# lifts the park on a rejection so the solver can act on it, and if this
# disagreed the solver would be handed an issue only to park it again.
pr_changes_requested() { # $1 = pr number, $2 = head sha
  "${FORGE[@]}" review-verdicts --number "$1" 2>/dev/null \
  | BOT="$BOT_LOGIN" SHA="$2" python3 -c "
import sys, json, os
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import approval_release
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
got = approval_release.changes_requested(
    verdicts=rows if isinstance(rows, list) else [],
    bot=os.environ['BOT'], sha=os.environ.get('SHA', ''))
print((got or {}).get('who') or '')
"
}

# WHO is asked to sign off, in order:
#   1. the person who FILED the issue — they asked for this, they judge it;
#   2. the repo owner, when the filer was the bot itself. The tester files its
#      own findings, and the bot approving the bot is not a sign-off.
# Prints a login, or nothing when neither can be read.
#
# Deliberately NOT the same choice as ISSUE_AUTHOR above, which is pinned to
# the repo owner so the @-mention target stays stable across bot-filed issues.
# A mention is "look at this"; a review request is "this is yours to decide",
# and those are not always the same person.
resolve_review_target() {
  local filed_by
  filed_by="$("${FORGE[@]}" issue --number "$ISSUE_NUM" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    print((json.load(sys.stdin) or {}).get('author') or '')
except Exception:
    print('')
" 2>/dev/null)"
  if [ -n "$filed_by" ] && [ "$(printf '%s' "$filed_by" | tr 'A-Z' 'a-z')" \
       != "$(printf '%s' "$BOT_LOGIN" | tr 'A-Z' 'a-z')" ]; then
    printf '%s' "$filed_by"
    return 0
  fi
  repo_owner_login 2>/dev/null
}

request_merge_approval() { # $1 = pr number, $2 = head sha
  local pr="$1" sha="$2" asked="" owner
  owner="$(resolve_review_target)"
  [ -f "$APPROVAL_ASKED_FILE" ] && asked="$(cat "$APPROVAL_ASKED_FILE" 2>/dev/null)"
  if [ "$asked" = "$sha" ]; then
    # Already asked, still waiting. Re-park anyway: `park_on_hold` is a no-op
    # when the label is already there, and this path is how the park comes
    # back if somebody took the label off without signing off — otherwise the
    # issue is workable forever, spawning a runner every tick to reach this
    # same line and do nothing.
    park_on_hold
    echo "[approval-gate] sign-off for ${sha:0:8} already requested — waiting"
    return 0
  fi
  if [ -z "$owner" ]; then
    # Nobody to ask. NOT merging is the only safe reading of the label: it
    # says a human decides, and "no human found" is not that human saying yes.
    echo "[approval-gate] the approval label is set but no owner could be resolved — not merging PR #$pr"
    return 1
  fi

  # Put them in the Reviewers box as well as @-mentioning them, matching the
  # GitLab runner (fixer-runner-gitlab.sh sets reviewer_ids[] here). The
  # mention is a notification that scrolls away; the review request is state
  # that sits on the pull request until it is answered, and it is what the
  # user's own review queue is built from.
  #
  # Safe against rule 9: this runs only once CI is all-green and the
  # autonomous review has approved this sha, which is exactly the condition
  # enforce_no_reviewer_when_ci_red permits. Best-effort — a host that
  # refuses the request must not stop the ask from being posted.
  if "${FORGE[@]}" request-review --number "$pr" --reviewers "$owner" \
       >/dev/null 2>&1; then
    echo "[approval-gate] requested @$owner as reviewer on PR #$pr"
  else
    echo "[approval-gate] WARNING: could not set @$owner as reviewer on PR #$pr — asking by comment only"
  fi

  post_issue_comment "🛂 MERGE APPROVAL REQUESTED (sha \`${sha:0:8}\`)

@$owner — this issue is labelled \`approval\`, so I will not merge it without you. Everything else is done: the checks are green, there are no conflicts, PR #$pr is not a draft, and the autonomous review approved \`${sha:0:8}\`. I have requested you as a reviewer on it.

To let it land, **approve PR #$pr** — the review button, or just say \`LGTM\` here. Either one lifts the \`On Hold\` label by itself; you do not have to @-mention me for this one. If you want changes first, say so here and @-mention \`@$BOT_LOGIN\`. If I push another commit I will ask again — an approval covers the code it was given for." \
    && echo "[approval-gate] asked @$owner to sign off on ${sha:0:8}" \
    || echo "[approval-gate] WARNING: could not post the approval request"
  printf '%s' "$sha" > "$APPROVAL_ASKED_FILE"
  # The wait is now on a person and can last days, so SAY so on the issue.
  #
  # This used to touch the marker alone. The marker lives on a volume only
  # this pod can read: it ranks the issue LAST so the repo's single slot goes
  # to work the bot can actually move, which is right — but ranked last in a
  # repo with a dozen open issues is ranked never, and from the outside the
  # issue still read `status::in-progress`. Nobody could see whose turn it
  # was. `park_on_hold` writes the same wait where the person can see it, and
  # the planner lifts it again the moment they approve.
  park_on_hold
}

approval_gate() { # $1 = pr number, $2 = head sha
  if ! approval_required; then
    # Label removed → the wait is stale, and so is the park that went with it.
    rm -f "$APPROVAL_ASKED_FILE" "$AWAITING_HUMAN_MARKER" 2>/dev/null
    return 0
  fi
  local pr="$1" sha="$2" granted="" who
  [ -z "$sha" ] && { echo "[approval-gate] could not resolve the head sha — not merging"; return 1; }
  [ -f "$APPROVAL_GRANTED_FILE" ] && granted="$(cat "$APPROVAL_GRANTED_FILE" 2>/dev/null)"
  if [ "$granted" = "$sha" ]; then
    echo "[approval-gate] sign-off already on record for ${sha:0:8} — merge may proceed"
    return 0
  fi
  who="$(pr_human_approval "$pr" "$sha")"
  if [ -n "$who" ]; then
    printf '%s' "$sha" > "$APPROVAL_GRANTED_FILE"
    rm -f "$APPROVAL_ASKED_FILE" "$AWAITING_HUMAN_MARKER" 2>/dev/null
    echo "[approval-gate] @$who approved ${sha:0:8} — merge may proceed"
    return 0
  fi
  # A REJECTION IS NOT A WAIT. Somebody read the change and asked for
  # something different: that is the solver's work again, so do not ask them
  # to sign off on what they just refused, and above all do not park. Parking
  # here would hold the issue until they replied a second time to say what
  # they had already said.
  who="$(pr_changes_requested "$pr" "$sha")"
  if [ -n "$who" ]; then
    rm -f "$APPROVAL_ASKED_FILE" "$AWAITING_HUMAN_MARKER" 2>/dev/null
    echo "[approval-gate] @$who requested changes on ${sha:0:8} — addressing the review, not merging"
    return 1
  fi
  request_merge_approval "$pr" "$sha"
  echo "[approval-gate] waiting for a human sign-off on ${sha:0:8}"
  return 1
}


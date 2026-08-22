# -- work-item status --------------------------------------------------
# GitHub has two states, open and closed, and the solver needs five answers —
# see builder/issue_status.py. Non-terminal statuses live in a `status::`
# label, terminal ones in GitHub's own close reason, which is the one place
# GitHub is genuinely better than the model it replaces: it records the
# operator's intent at the moment of closing and never overwrites it.

post_issue_comment() { # $1 = body
  local _bodyf _rc
  _bodyf="$(mktemp)"
  printf '%s' "$1" > "$_bodyf"
  "${FORGE[@]}" comment --number "$ISSUE_NUM" --body-file "$_bodyf" \
    >/dev/null 2>&1
  _rc=$?
  rm -f "$_bodyf"
  return $_rc
}

# Say something ON THE PULL REQUEST. A different call from
# `post_issue_comment`, not a variant of it: GitHub models a pull request as
# an issue and the two happen to coincide there, while on GitLab the same
# number addresses an unrelated issue. The forge keeps them apart; this must
# too.
post_pr_comment() { # $1 = pr number, $2 = body
  local _bodyf _rc
  _bodyf="$(mktemp)"
  printf '%s' "$2" > "$_bodyf"
  "${FORGE[@]}" comment-on-change-request --number "$1" --body-file "$_bodyf" \
    >/dev/null 2>&1
  _rc=$?
  rm -f "$_bodyf"
  return $_rc
}

# Move the issue to a NON-TERMINAL status. Refuses on a closed issue.
#
# That refusal is the important half. GitHub reopens a closed issue as a side
# effect of some writes, and a status the wrapper sets out of habit on a tick
# after the work landed would resurrect an issue a human deliberately ended —
# silently, five minutes after they closed it, over and over. Nothing here may
# be the reason a closed issue comes back.
set_issue_status() { # $1 = status name (issue_status vocabulary)
  local want="$1"
  if [ "${ISSUE_STATE:-open}" = "closed" ]; then
    echo "[status] #$ISSUE_NUM is closed — NOT setting '$want' (that would reopen it)"
    return 0
  fi
  local updates
  updates="$(LABELS="$ISSUE_LABELS_JSON" WANT="$want" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import issue_status
labels = json.loads(os.environ['LABELS'] or '[]')
add, remove = issue_status.label_updates(labels, os.environ['WANT'])
print(json.dumps({'add': add, 'remove': remove}))
" 2>/dev/null)"
  [ -n "$updates" ] || { echo "[status] could not compute the label diff for '$want'"; return 0; }
  # An empty diff means the issue already says this. Writing anyway would
  # append a timeline event on every five-minute tick, and an issue whose
  # history is a wall of identical label events is an issue nobody can read.
  local add remove
  add="$(printf '%s' "$updates" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin)['add']))")"
  remove="$(printf '%s' "$updates" | python3 -c "import sys,json;print('\n'.join(json.load(sys.stdin)['remove']))")"
  local add_csv
  add_csv="$(printf '%s' "$updates" | python3 -c "import sys,json;print(','.join(json.load(sys.stdin)['add']))" 2>/dev/null)"
  if [ -n "$add_csv" ]; then
    if "${FORGE[@]}" add-labels --number "$ISSUE_NUM" --labels "$add_csv"; then
      echo "[status] #$ISSUE_NUM → $want"
    else
      echo "[status] WARNING: could not set '$want' on #$ISSUE_NUM"
    fi
  fi
  # Applying BOTH halves of the diff is what keeps two `status::` labels off
  # one issue: no host here enforces one-value-per-scope, so the rule is the
  # diff, and skipping the removals leaves the previous status in place beside
  # the new one. How a removal is spelled — a URL naming the label, a field in
  # an update — is the forge's business now.
  while IFS= read -r stale; do
    [ -n "$stale" ] || continue
    "${FORGE[@]}" remove-label --number "$ISSUE_NUM" --label "$stale" \
      >/dev/null 2>&1 || true
  done <<< "$remove"
}

# Close the issue with the reason a TERMINAL status implies: `completed` for a
# delivery, `not_planned` for a revoke. The distinction is the whole reason
# terminal status lives in the close reason — "was this delivered?" has to be
# answerable afterwards without re-deriving it from the merge history.
close_issue_as() { # $1 = terminal status name
  # The INTENT, not a field name. One host has a native close reason and the
  # other writes the intent as a label; which of the two is in use is not
  # something this runner is in a position to know, and it no longer has to.
  local intent
  intent="$(WANT="$1" python3 -c "
import os, sys
sys.path.insert(0, os.environ.get('PYTHONPATH','').split(os.pathsep)[0])
import issue_status
want = os.environ['WANT']
if want in issue_status.TERMINAL:
    print('delivered' if want == issue_status.DONE else 'revoked')
" 2>/dev/null)"
  [ -n "$intent" ] || { echo "[status] '$1' is not a terminal status — not closing"; return 1; }
  if [ "${ISSUE_STATE:-open}" = "closed" ]; then
    echo "[status] #$ISSUE_NUM already closed — leaving its close reason alone"
    return 0
  fi
  if "${FORGE[@]}" close-issue --number "$ISSUE_NUM" "--$intent" >/dev/null 2>&1; then
    echo "[status] closed #$ISSUE_NUM as $1 ($intent)"
    ISSUE_STATE=closed
    [ "$intent" = "revoked" ] && abandon_open_change_requests
    return 0
  fi
  echo "[status] WARNING: could not close #$ISSUE_NUM"
  return 1
}

# The issue was CALLED OFF, so the work opened for it is not going to land.
#
# Only on `revoked`, never on `delivered`: a delivered issue was closed BY its
# merge, and its branch is already gone through `merge(delete_branch=True)`.
# A revoked one leaves an open change request nobody will ever merge and a
# branch nobody will ever touch — which is what happened on
# k8s-ultimate-web-stack#93, where the issue was closed by hand and a person
# had to be told to check for leftovers because the bot could not.
#
# Order matters and is not arbitrary: close the change request FIRST, then
# delete the branch. Deleting first would close it as an unreachable diff and
# lose the review history's context. If the close fails, the branch stays —
# an open change request pointing at a deleted branch is worse than either.
#
# The default branch is refused inside the forge, on both hosts, rather than
# being trusted to any caller here.
abandon_open_change_requests() {
  local prs pr ref
  prs="$("${FORGE[@]}" change-requests-for-issue --number "$ISSUE_NUM" 2>/dev/null \
        | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
print(' '.join(str(n) for n in rows if isinstance(n, int)))
" 2>/dev/null)"
  [ -n "${prs:-}" ] || return 0
  for pr in $prs; do
    ref="$(head_ref_of_pr "$pr" 2>/dev/null)"
    post_issue_comment "🚮 Closing $CR_NOUN #$pr without merging — #$ISSUE_NUM was closed as not-doing, so this work is not going to land." >/dev/null 2>&1 || true
    if "${FORGE[@]}" close-change-request --number "$pr" >/dev/null 2>&1; then
      echo "[abandon] closed $CR_NOUN #$pr (issue revoked)"
    else
      echo "[abandon] WARNING: could not close $CR_NOUN #$pr — leaving its branch alone"
      continue
    fi
    [ -n "${ref:-}" ] || { echo "[abandon] no branch recorded for #$pr"; continue; }
    if "${FORGE[@]}" delete-branch --branch "$ref" >/dev/null 2>&1; then
      echo "[abandon] deleted branch '$ref'"
    else
      echo "[abandon] left branch '$ref' in place (refused or already gone)"
    fi
  done
}


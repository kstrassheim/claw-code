#!/bin/bash
# fixer-runner: invoked as a backgrounded subprocess inside the openclaw
# container by cron-issue-spawn. Holds a per-repo lock so only ONE
# fixer at a time touches a given repo's checkout, manages the shared
# git working tree under ~/.openclaw/projects/<repo>/, runs the
# `openclaw agent --local` turn, and releases the lock on exit.
#
# Args:
#   $1 repo full_name        (e.g. owner/name)
#   $2 issue number
#   $3 issue url             (https://github.com/owner/name/issues/N)
#   $4 issue title           (free text — used in the agent prompt)
#
# Lock semantics: `mkdir <lockdir>` is atomic on local filesystems, so
# the first runner to call it wins. Subsequent runners exit fast. The
# trap clears the lock on every exit path (normal, signal, agent
# crash) — but operators can `rm -rf` the lockdir if a runner is
# killed before its trap fires.
#
# Logging: stdout+stderr are appended to a per-(repo,issue) file under
# ~/.openclaw/fixer-logs/. `watcher list` / debugging inspect those.
set -uo pipefail

REPO="$1"
ISSUE_NUM="$2"
ISSUE_URL="$3"
ISSUE_TITLE="$4"

STATE_ROOT="${HOME:-/home/node}/.openclaw"
PROJECTS_ROOT="$STATE_ROOT/projects"
PROJECT_DIR="$PROJECTS_ROOT/$REPO"
# Lock dirs are siblings of the project tree, NOT inside it — having
# the lock inside the project dir broke `git clone` because the
# destination directory was non-empty.
LOCK_ROOT="$STATE_ROOT/.fixer-locks"
LOCK_DIR="$LOCK_ROOT/${REPO//\//__}"
LOG_DIR="$STATE_ROOT/fixer-logs"
LOG_FILE="$LOG_DIR/${REPO//\//_}-${ISSUE_NUM}.log"

mkdir -p "$LOG_DIR" "$LOCK_ROOT" "$(dirname "$PROJECT_DIR")"

# Acquire the per-repo lock. `mkdir` is atomic on local filesystems —
# the first runner that asks for it wins; everyone else exits fast.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date -Iseconds)] lock held for $REPO; aborting fixer for #$ISSUE_NUM" >> "$LOG_FILE"
  exit 0
fi
echo "$BASHPID $(date -Iseconds) issue=$ISSUE_NUM" > "$LOCK_DIR/owner"

# Defensive cleanup: a previous build (.22/.23) put .fixer.lock
# INSIDE the project dir, which leaves the dir non-empty and breaks
# the clone path below. Strip any leftover .fixer.lock from the
# legacy location so the clone-or-update logic can proceed cleanly.
rm -rf "$PROJECT_DIR/.fixer.lock" 2>/dev/null

trap 'rm -rf "$LOCK_DIR"' EXIT

{
  echo "============================================================"
  echo "[$(date -Iseconds)] fixer start  repo=$REPO  issue=#$ISSUE_NUM"
  echo "============================================================"

  # Ensure a clean checkout pinned to origin/main. If the repo doesn't
  # exist yet, clone it. If it does, fetch + hard-reset to origin/main.
  if [ ! -d "$PROJECT_DIR/.git" ]; then
    echo "[clone] $REPO → $PROJECT_DIR"
    git clone --quiet "https://github.com/$REPO.git" "$PROJECT_DIR"
  fi

  cd "$PROJECT_DIR"
  git fetch --quiet origin
  # Detect the default branch (don't assume main).
  DEFAULT_BRANCH="$(git remote show origin | awk '/HEAD branch/ {print $NF}')"
  echo "[checkout] default-branch=$DEFAULT_BRANCH"
  git checkout --quiet "$DEFAULT_BRANCH"
  git reset --hard --quiet "origin/$DEFAULT_BRANCH"
  # Clean any untracked leftovers from a previous (killed) fixer.
  git clean -fdx --quiet

  BRANCH="issue-$ISSUE_NUM-fix"
  # If a stale branch exists locally, drop it; the agent will recreate.
  git branch -D "$BRANCH" 2>/dev/null || true
  git checkout --quiet -b "$BRANCH"

  # Hand off to the embedded agent. cwd is already $PROJECT_DIR.
  echo "[agent] starting openclaw agent --local"
  # Fresh session-id per attempt so context doesn't accumulate across
  # retries (a wedged previous run can leave 60+ messages in the
  # session file, which then trips the context-overflow precheck on
  # every subsequent attempt). Timestamp suffix keeps each fixer
  # invocation isolated; old session files age out via openclaw's
  # own retention.
  SESSION_ID="issue-${REPO//\//-}-${ISSUE_NUM}-$(date +%s)"
  openclaw agent --local \
    --timeout 3500 \
    --session-id "$SESSION_ID" \
    --message "You are in a fresh checkout of $REPO at $(pwd) on branch $BRANCH (already branched off $DEFAULT_BRANCH). Fix issue $ISSUE_URL — \"$ISSUE_TITLE\". Steps: (1) implement the change, (2) commit with a descriptive message, (3) push the branch (\`git push -u origin $BRANCH\`), (4) open a PR back to $DEFAULT_BRANCH with \"Closes #$ISSUE_NUM\" in the body, then stop. Do not delegate to subagents. Do not ask the user for confirmation. cameron-claw is the git author identity (already configured via \$GITHUB_TOKEN)."
  AGENT_EXIT=$?

  echo "[$(date -Iseconds)] fixer done   repo=$REPO  issue=#$ISSUE_NUM  agent-exit=$AGENT_EXIT"
} >> "$LOG_FILE" 2>&1

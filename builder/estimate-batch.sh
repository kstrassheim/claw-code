#!/bin/bash
# estimate-batch: size several issues of ONE repository, one after another.
#
#   estimate-batch <owner/repo> <issue> [<issue> ...]
#
# Spawned by cron-issue-spawn for the `toEstimate` list the planner hands out:
# every assigned issue that has no size yet, or carries the `estimate` request,
# is sized BEFORE the repository starts any new work (heartbeat-issue-tick).
#
# WHY A BATCH, AND WHY SEQUENTIAL
# -------------------------------
# estimate-runner needs no checkout and no repo lock, so sizing is not bound by
# the repo's single fixer slot. But every estimate is a full agent process on
# the gateway pod, and ten of them at once would crowd it. So the whole list
# goes to ONE batch that runs them in order. A failed estimate is logged and
# the batch moves on: one bad issue must not leave the rest unsized.
#
# THE LOCK
# --------
# ~/.openclaw/.estimate-locks/<owner>__<repo>/ (every `/` written as `__`, so
# GitLab subgroups work), with the owning PID in `owner` — the same shape as the
# fixer lock, so the planner reads both with one helper and does not send a
# second batch while this one is still running. A lock whose owner is gone, or
# that is older than the TTL, is stale and taken over: pod restarts are normal.
#
# estimate-runner itself exits early for an issue that is already sized and was
# not asked to be re-sized, so an issue sized while it waited in this queue
# costs nothing.
set -uo pipefail

REPO="${1:?usage: estimate-batch <owner/repo> <issue> [<issue> ...]}"
shift
[ "$#" -gt 0 ] || { echo "estimate-batch: no issues given" >&2; exit 2; }

BASE="${HOME:-/home/node}/.openclaw"
LOCK_ROOT="$BASE/.estimate-locks"
LOCK="$LOCK_ROOT/${REPO//\//__}"
TTL="${ESTIMATE_BATCH_TTL_SECONDS:-3600}"
LOG_DIR="$BASE/estimate-logs"
LOG="$LOG_DIR/batch-${REPO//\//_}.log"

mkdir -p "$LOCK_ROOT" "$LOG_DIR"
exec >>"$LOG" 2>&1

echo "============================================================"
echo "[$(date -Iseconds)] estimate-batch start  repo=$REPO  issues=$*"

if ! mkdir "$LOCK" 2>/dev/null; then
  holder=$(awk 'NR==1{print $1}' "$LOCK/owner" 2>/dev/null || true)
  age=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null && [ "$age" -lt "$TTL" ]; then
    echo "[batch] another batch (pid $holder, ${age}s old) is sizing $REPO — exiting"
    exit 0
  fi
  echo "[batch] taking over a stale lock (owner '${holder:-none}', ${age}s old)"
  rm -rf "$LOCK"
  mkdir "$LOCK" || { echo "[batch] could not take the lock — exiting"; exit 1; }
fi
echo "$$ estimate-batch $REPO" > "$LOCK/owner"
trap 'rm -rf "$LOCK"' EXIT

done_n=0
failed_n=0
for n in "$@"; do
  # Bare name: PATH puts the ConfigMap copy first (see cron-issue-spawn).
  if estimate-runner "$REPO" "$n"; then
    done_n=$((done_n + 1))
  else
    failed_n=$((failed_n + 1))
    echo "[batch] estimate for $REPO#$n failed (rc=$?) — continuing"
  fi
  # Refresh the lock's mtime so a long batch is not mistaken for a stale one.
  touch "$LOCK"
done

echo "[$(date -Iseconds)] estimate-batch exit  repo=$REPO  sized=$done_n  failed=$failed_n"
exit 0

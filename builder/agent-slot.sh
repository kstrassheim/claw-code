#!/bin/bash
# ---------------------------------------------------------------------------
# Shared concurrency gate for the three autonomous subsystems.
#
# WHY THIS EXISTS
# Concurrency against the Kimi Coding endpoint is not free. Measured live:
# 1, 2 and 3 simultaneous requests all return 200; 5 simultaneous requests
# return `429 The engine is currently overloaded` for ALL FIVE — crossing the
# ceiling does not queue the excess, it rejects everything in flight,
# including the sessions that were already working.
#
# The three subsystems used to run with no coordination at all, and their
# planners were scheduled on the same minute (issue-watcher and pr-reviewer
# both `*/5`, tester `*/10`), so at :00/:10/:20 all three launched together.
# The symptom was the pull-request reviewer taking a 429 on its very FIRST
# model call, dying in ~60s, and retrying into the identical collision five
# minutes later — seven times in a row — while the issue solver, which won the
# race, ran at a 2% failure rate.
#
# WHAT THIS GATE DOES *NOT* FIX (correction, 2026-07-25)
# An earlier version of this header said that pattern "reads exactly like a
# provider outage and is not one". That was wrong, and worth remembering
# before blaming concurrency again. Ten SEQUENTIAL probes — zero concurrency
# — returned 429/429/200/200/429/429/200/200/200/429. The endpoint rejects a
# large share of requests on its own, in bursts that vary by time of day
# (100% rejection 09:00-12:00 one day, clean that same afternoon). This gate
# removes the amplification we add; it cannot make an overloaded provider
# answer, and it does nothing whatsoever for a 403 `access_terminated_error`
# (billing quota exhausted), which halts every subsystem regardless of how
# politely they queue. Diagnose the failure before reaching for this file.
#
# Staggering the cron schedules separates the STARTS. It does not help once
# runs overlap, and they do: a tester run lasts ~40 min, a fixer ~20 min, a
# reviewer ~15 min. This gate is what actually holds the ceiling.
#
# HOW IT WORKS
# A slot directory per concurrent agent run, in the shared pod filesystem
# (all three subsystems are processes in the SAME pod, spawned via
# kubectl exec, so a plain filesystem semaphore is enough). `mkdir` is
# atomic on the underlying filesystem, which is what makes the claim safe
# without any lock daemon.
#
# Slots are reaped when their owner dies. The check is PID-liveness AND a
# cmdline match, not PID alone: after a pod restart a recycled PID could
# otherwise make a dead slot look permanently held, which would wedge every
# subsystem at once — the exact failure this file is meant to prevent.
#
# USAGE
#   . /usr/local/bin/agent-slot
#   SLOT_NAME=reviewer
#   if ! acquire_agent_slot; then ...yield this tick, cleanly... fi
#   ...invoke the agent...
#   release_agent_slot        # also safe to call from an EXIT trap
#
# Acquire IMMEDIATELY BEFORE the agent invocation, never earlier: holding a
# slot through a git clone or a 30-minute pipeline wait would starve the
# other subsystems for work that never touches the model.
#
# A caller that cannot get a slot must yield WITHOUT recording progress —
# no attempt counted, no HEAD marked tested, no verdict posted. Yielding is
# not a failure, it is "not my turn yet"; the next tick retries.
# ---------------------------------------------------------------------------

AGENT_SLOT_DIR="${AGENT_SLOT_DIR:-${STATE_ROOT:-$HOME/.openclaw}/.agent-slots}"
# 2, not the measured 3: leave headroom. An agent turn is not always exactly
# one in-flight request (tool-call fan-out, image description and summarisation
# can overlap), and being one under the ceiling costs a little throughput while
# being one over costs every run currently in flight.
MAX_AGENT_SLOTS="${MAX_AGENT_SLOTS:-2}"
# How long to wait for a free slot before yielding the tick.
AGENT_SLOT_WAIT="${AGENT_SLOT_WAIT:-90}"
AGENT_SLOT=""

# PRIORITY: the issue solver and the pull-request reviewer outrank the
# deployment tester, across every project.
#
# The gate was first-come-first-served, and the tester is the worst possible
# winner of that race: its run is the longest of the three (~40-60 min against
# ~20 for a fixer), so one tester that happens to start first can sit on a
# slot for an hour while issues and pull requests queue behind it. That is a
# lot of tokens spent on re-testing a main commit while actual work waits.
#
# A low-priority caller may only take a slot if AGENT_SLOT_RESERVED slots
# remain free for high-priority work afterwards. With the default 2 slots and
# 1 reserved, that means the tester starts only when the pod is otherwise
# idle, and yields immediately rather than waiting — a yield costs nothing,
# and the commit it skipped is NOT recorded as tested, so the next tick picks
# it up again (see the tester's early-exit path).
#
# What this does NOT do: preempt. A tester already holding a slot keeps it
# until its run ends or its `agent-limits` cap fires. Priority decides who
# STARTS, not who is interrupted mid-run.
AGENT_SLOT_RESERVED="${AGENT_SLOT_RESERVED:-1}"
# high (default) = solver, reviewer · low = tester
SLOT_PRIORITY="${SLOT_PRIORITY:-high}"

# The runners allowed to hold a slot, matched against the owner's cmdline.
#
# Substrings, not exact names, and deliberately so: the image installs these
# WITHOUT their .sh suffix (`/usr/local/bin/fixer-runner`) while the repo keeps
# it (`builder/fixer-runner.sh`), and the cmdline carries a path in front of
# either. Matching the stem covers both spellings.
#
# Keep this list in step with the runners in builder/ — one missing from it has
# its slot reaped out from under it while it is still working, and the reap is
# silent, because a freed slot looks exactly like a slot nobody wanted. That is
# the double-booking this whole file exists to prevent.
AGENT_SLOT_OWNERS="${AGENT_SLOT_OWNERS:-fixer-runner|tester-runner|reviewer-runner}"

# A slot is stale unless its owner PID is alive AND still looks like one of
# our runners.
_slot_owner_alive() {
  _pid="${1:-}"
  [ -n "$_pid" ] || return 1
  kill -0 "$_pid" 2>/dev/null || return 1
  tr '\0' ' ' < "/proc/$_pid/cmdline" 2>/dev/null \
    | grep -qE "$AGENT_SLOT_OWNERS" || return 1
  return 0
}

# Kill openclaw-agent processes that outlived ANY legitimate turn.
#
# The runners already reap the agent's process group when a turn ends, but
# `openclaw agent` re-sessions itself, so the actual `openclaw-agent` survives
# the group kill and re-parents to init (ps shows ppid=1 for live AND dead
# ones alike, so ppid is useless as a discriminator). Each survivor holds
# ~400-580MB; three of them were sitting on 1.4GB of the pod's 5.5GB cgroup
# limit, growing by roughly one orphan per frontend issue (those runs start
# vite + chromium, which is what keeps the process alive). Left alone this
# ends in an OOM kill of the whole pod.
#
# AGE is the safe discriminator: a turn is hard-killed at
# AGENT_TURN_TIMEOUT+120s by run_agent_turn / the tester's watchdog, so
# anything older than +300s cannot be a live turn. Killing the session (-PID)
# rather than the pid also takes the chromium/vite children with it.
reap_orphan_agents() {
  _max_age=$(( ${AGENT_TURN_TIMEOUT:-${AGENT_TIMEOUT:-3500}} + 300 ))
  for _p in $(pgrep -x openclaw-agent 2>/dev/null); do
    _secs="$(ps -o etimes= -p "$_p" 2>/dev/null | tr -d ' ')"
    [ -n "${_secs:-}" ] || continue
    if [ "$_secs" -gt "$_max_age" ]; then
      echo "[reap] orphaned openclaw-agent pid=$_p age=${_secs}s (max ${_max_age}s) — killing its session"
      kill -KILL -"$_p" 2>/dev/null || kill -KILL "$_p" 2>/dev/null || true
    fi
  done
}

_reap_stale_slots() {
  for _d in "$AGENT_SLOT_DIR"/slot-*; do
    [ -d "$_d" ] || continue
    if ! _slot_owner_alive "$(cat "$_d/pid" 2>/dev/null || echo)"; then
      echo "[slot] reaping stale slot $(basename "$_d") (owner $(cat "$_d/owner" 2>/dev/null || echo '?') gone)"
      rm -rf "$_d" 2>/dev/null || true
    fi
  done
}

# How many slots are currently unclaimed. Counted after a reap, so a slot
# whose owner died does not look busy.
_free_slot_count() {
  _n=0 _j=1
  while [ "$_j" -le "$MAX_AGENT_SLOTS" ]; do
    [ -d "$AGENT_SLOT_DIR/slot-$_j" ] || _n=$(( _n + 1 ))
    _j=$(( _j + 1 ))
  done
  printf '%s\n' "$_n"
}

# 0 = slot acquired (AGENT_SLOT set), 1 = none free within the wait budget.
acquire_agent_slot() {
  _waited=0
  mkdir -p "$AGENT_SLOT_DIR" 2>/dev/null || true
  # Every run passes through here before touching the model, so this is the
  # natural place to sweep leaked agents — no extra cron, and it runs often.
  reap_orphan_agents
  while :; do
    _reap_stale_slots
    # Low priority yields the whole tick rather than queueing: waiting would
    # hold the tester process open for nothing, and skipping is cheap.
    if [ "$SLOT_PRIORITY" = "low" ]; then
      _free="$(_free_slot_count)"
      if [ "$_free" -le "$AGENT_SLOT_RESERVED" ]; then
        echo "[slot] yielding: ${SLOT_NAME:-agent} is low priority, $_free/$MAX_AGENT_SLOTS free" \
             "and $AGENT_SLOT_RESERVED reserved for the issue solver and reviewer" \
             "(held by: $(cat "$AGENT_SLOT_DIR"/slot-*/owner 2>/dev/null | tr '\n' ' '))"
        return 1
      fi
    fi
    _i=1
    while [ "$_i" -le "$MAX_AGENT_SLOTS" ]; do
      if mkdir "$AGENT_SLOT_DIR/slot-$_i" 2>/dev/null; then
        printf '%s\n' "$$" > "$AGENT_SLOT_DIR/slot-$_i/pid"
        printf '%s\n' "${SLOT_NAME:-agent}" > "$AGENT_SLOT_DIR/slot-$_i/owner"
        AGENT_SLOT="$AGENT_SLOT_DIR/slot-$_i"
        echo "[slot] acquired slot $_i/$MAX_AGENT_SLOTS for ${SLOT_NAME:-agent}"
        return 0
      fi
      _i=$(( _i + 1 ))
    done
    if [ "$_waited" -ge "$AGENT_SLOT_WAIT" ]; then
      echo "[slot] all $MAX_AGENT_SLOTS slots busy after ${_waited}s (held by: $(cat "$AGENT_SLOT_DIR"/slot-*/owner 2>/dev/null | tr '\n' ' '))"
      return 1
    fi
    sleep 5
    _waited=$(( _waited + 5 ))
  done
}

release_agent_slot() {
  [ -n "${AGENT_SLOT:-}" ] || return 0
  rm -rf "$AGENT_SLOT" 2>/dev/null || true
  echo "[slot] released $(basename "$AGENT_SLOT")"
  AGENT_SLOT=""
}

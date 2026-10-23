#!/bin/bash
# ---------------------------------------------------------------------------
# A state directory of its own for every `openclaw agent --local` session.
#
# WHY THIS EXISTS
# openclaw 2026.10 refuses `agent --local` while a Gateway runs on the same
# state directory:
#
#   A Gateway is running for this state directory (pid 6, port 18789). Run
#   without --local to use it, or stop the Gateway first
#
# The Gateway always runs in this pod, on ~/.openclaw, and every runner is
# `openclaw agent --local` on ~/.openclaw — so after the v2026.10.4 upgrade
# every estimate, solver, reviewer and tester turn died before the model was
# called, and the planners re-spawned them every tick to die again. Nothing
# failed loudly: the pod was healthy, the CronJobs completed, and the only
# trace was "produced no POINTS line" in the estimate log.
#
# WHY NOT THE TWO WAYS THE MESSAGE OFFERS
#   - Through the Gateway (no --local): the turn would run inside the Gateway
#     process, so the runner's cwd, its environment and the process group it
#     kills when the turn ends would no longer be the agent's. Dev servers a
#     turn starts would outlive it as children of the Gateway — the leak
#     `kill_agent_group` exists to stop.
#   - `openclaw agent exec`: one isolated turn, but every call is a new
#     session (the solver's later turns continue the first one), the agent's
#     workspace becomes --cwd (TOOLS.md and the mounted skills live in
#     ~/.openclaw/workspace), and nothing is logged until the turn ends (the
#     reviewer stops the agent on a streamed `ended with stopReason=` line).
#
# WHAT THIS DOES
# Keeps `--local` and everything the runners rely on, and moves only what the
# lock is about. Both Gateway locks live under the state directory, so a turn
# on a different OPENCLAW_STATE_DIR does not collide with the Gateway. The
# config and the workspace stay the Gateway's own, named explicitly, because
# both default to paths under the state directory:
#
#   OPENCLAW_STATE_DIR      $AGENT_STATE_RUNS_DIR/<session id>
#   OPENCLAW_CONFIG_PATH    ~/.openclaw/openclaw.json
#   OPENCLAW_WORKSPACE_DIR  ~/.openclaw/workspace
#
# Checked against ghcr.io/openclaw/openclaw:2026.10.4 with a Gateway running:
# the plain `--local` turn is refused, this one reaches the model; the
# workspace used is ~/.openclaw/workspace (no second one is seeded); and a
# Gateway client started from inside the turn (`openclaw message send`,
# `openclaw health`) still reaches the Gateway, because it finds it through
# the shared config.
#
# ONE DIRECTORY PER SESSION, NOT PER CALL: the solver runs several turns on
# one --session-id and each continues the last, so its turns must find the
# same session store. Every runner's session id already carries a timestamp,
# so one id is one run.
#
# WHAT A TURN DOES NOT SEE: plugins. Install records live in the state
# database, so a plugin installed on the Gateway's state (kimi-provider) is not
# loaded in the turn. Without it kimi/k3 runs on the core thinking levels,
# which render-config already treats as a survivable degradation.
#
# USAGE
#   . /usr/local/bin/agent-state.sh
#   agent_state_env "$SESSION_ID"
#   "${AGENT_STATE_ENV[@]}" openclaw agent --local --session-id "$SESSION_ID" ...
#
# A prefix and not an export, so that only the agent sees the redirected
# state: every other openclaw call the runner makes keeps the Gateway's.
# `env` execs its command in place, so the PID, $! and any process group the
# caller sets up with setsid are exactly what they were without it.
# ---------------------------------------------------------------------------

AGENT_STATE_RUNS_DIR="${AGENT_STATE_RUNS_DIR:-${TMPDIR:-/tmp}/openclaw-agent-runs}"
# Longer than any one run, so a session is never swept between its own turns.
# Each turn touches its directory, so this is the idle time since the LAST
# turn, not the age of the run.
AGENT_STATE_MAX_IDLE_MIN="${AGENT_STATE_MAX_IDLE_MIN:-1440}"

AGENT_STATE_ENV=()

agent_state_env() { # $1 = session id
  local main="${STATE_ROOT:-${HOME:-/home/node}/.openclaw}"
  local name dir
  name="$(printf '%s' "${1:-run-$$}" | tr -c 'A-Za-z0-9._-' '_')"
  dir="$AGENT_STATE_RUNS_DIR/$name"
  AGENT_STATE_ENV=()
  mkdir -p "$dir" || return 1
  touch "$dir"
  # Sessions that finished long ago. A directory holds one session's SQLite
  # store and transcript; nothing reads it once the run is over.
  find "$AGENT_STATE_RUNS_DIR" -mindepth 1 -maxdepth 1 -type d \
    -mmin +"$AGENT_STATE_MAX_IDLE_MIN" -exec rm -rf {} + 2>/dev/null || true
  AGENT_STATE_ENV=(env
    "OPENCLAW_STATE_DIR=$dir"
    "OPENCLAW_CONFIG_PATH=$main/openclaw.json"
    "OPENCLAW_WORKSPACE_DIR=$main/workspace")
}

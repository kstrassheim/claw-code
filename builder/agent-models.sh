# shellcheck shell=bash
# agent-models: which LLM does each subsystem run on?
#
# Sourced by the three runners — fixer, tester, reviewer — exactly like
# agent-limits.sh, and written by /usr/local/bin/agent-models, which the
# `developer`, `tester` and `reviewer` chat skills call.
#
# WHY THIS EXISTS
# ---------------
# There was only ever ONE model. `agents.defaults.model.primary` in the
# ConfigMap named it (kimi/k3), the render step demoted it to MiniMax or
# Mistral only when a key was missing, and the runners called `openclaw agent`
# with no --model at all — silently inheriting whatever chat used.
#
# So "run the autonomous runners on the cheaper model" had exactly one lever:
# drop MOONSHOT_API_KEY at deploy time. That strips Kimi from the config
# entirely, takes the chat down with it, and leaves `models` unable to switch
# back. All-or-nothing, and a redeploy either way.
#
# `openclaw agent --model <id>` accepts a per-run override, so the choice can
# be per subsystem and per environment instead. dev and prod have separate
# workspace PVCs, which is the whole environment split: setting a model here on
# dev cannot reach prod, with no per-environment YAML.
#
# WHAT THE KEYS MEAN
# ------------------
#   solver          the issue solver's agent turns
#   solver.small    implementing a story at or below solver.small.max_points
#   planning        story-point estimation and sprint planning
#   tester          the deployment tester's run
#   reviewer        the pull-request reviewer's run
#   reviewer.small  reviewing such a story's pull request — same threshold
#
# The two `.small` keys are the cheap lane, and each is read with
# agent_model_raw(): unset means that half is OFF, not "inherit the baseline".
#
# The value is a model id as `openclaw models list` prints it
# (`minimax/MiniMax-M3`, `kimi/k3`), or the word `default`, which means: pass
# no --model and inherit the global default. `default` is the built-in for
# every key, so an untouched deployment behaves exactly as before this file
# existed.
#
# FAIL-SOFT, AND WHERE THE STRICTNESS LIVES
# -----------------------------------------
# A bad model id is far more dangerous than a bad timeout: every single run
# would die at startup, and the planner would respawn them forever. So the
# strict check — "is this model real, and is it configured with credentials?"
# — runs in the CLI, where a human is present to read the refusal.
#
# Here, at read time in the runner, the check is only structural
# (provider/model, no whitespace). Anything else falls back to inheriting the
# global default, because a run on the wrong-but-working model is recoverable
# and a run that cannot start is not.

AGENT_MODELS_FILE="${AGENT_MODELS_FILE:-${HOME:-/home/node}/.openclaw/agent-models.conf}"

# The model a runner uses when no explicit choice is set for its key.
#
# Written at pod start by the render-config init container from the RENDERED
# config, and deliberately NOT read from openclaw.json: `openclaw models set`
# — the chat-side switch — rewrites agents.defaults.model.primary in that
# file, so a runner reading it would follow whatever the chat is currently on.
# Trying a different model in chat must not re-point the issue solver, the
# tester and the reviewer.
#
# Missing file (an older PVC, or a boot where the write failed) means empty,
# which means pass no --model — the pre-existing behaviour, so nothing breaks.
AGENT_MODEL_BASELINE_FILE="${AGENT_MODEL_BASELINE_FILE:-${HOME:-/home/node}/.openclaw/runner-model.default}"

# _am_wellformed <value> — provider/model, no spaces, nothing exotic.
# Deliberately loose: this is a guard against a mangled file, not a
# whitelist. The CLI already refused anything openclaw cannot resolve.
_am_wellformed() {
  case "${1:-}" in
    ''|*[[:space:]]*)   return 1 ;;
    */*/*)              return 1 ;;   # one slash, not a path
    */*)                ;;
    *)                  return 1 ;;   # no slash at all
  esac
  case "$1" in
    *[!A-Za-z0-9._/-]*) return 1 ;;
  esac
  return 0
}

# agent_model_baseline — the pinned deploy-time model, or empty.
agent_model_baseline() {
  [ -r "$AGENT_MODEL_BASELINE_FILE" ] || return 0
  _am_b="$(sed 's/^[[:space:]]*//; s/[[:space:]]*$//' "$AGENT_MODEL_BASELINE_FILE" 2>/dev/null | head -1)"
  [ -n "$_am_b" ] || return 0
  _am_wellformed "$_am_b" || return 0
  printf '%s\n' "$_am_b"
}

# agent_model <key> — the model this subsystem should run on.
#
# Resolution order: the explicit setting for the key, then the pinned
# baseline, then empty. Empty means "pass no --model" and is what every
# failure path produces, so a broken store can never stop a run — it can only
# make it behave the way it did before this file existed.
# agent_model_raw <key> — the value EXPLICITLY set for this key, or empty.
#
# No baseline fallback, which is the whole point: a caller that needs to know
# whether a key was configured at all cannot use agent_model(), because that
# answers with the baseline and is therefore almost never empty. Both halves of
# the cheap lane — the solver's and the reviewer's — ask exactly this question,
# "is a small-story model set?", and must not route small work somewhere
# different merely because a baseline exists.
agent_model_raw() {
  _am_key="$1"
  [ -r "$AGENT_MODELS_FILE" ] || return 0
  # Trim the ends only. Stripping ALL whitespace would fold "rm -rf /" into
  # "rm-rf/", which then satisfies the provider/model shape below — turning
  # garbage into a plausible id that fails at the first request instead of
  # being rejected here. Inner whitespace must survive to be caught.
  _am_raw="$(sed 's/#.*//' "$AGENT_MODELS_FILE" 2>/dev/null \
             | grep -E "^[[:space:]]*$(printf '%s' "$_am_key" | sed 's/\./\\./g')[[:space:]]*=" \
             | tail -1 | cut -d= -f2- \
             | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  [ -n "$_am_raw" ] || return 0
  case "$_am_raw" in
    default|inherit|auto) return 0 ;;   # "inherit" is not a setting
  esac
  _am_wellformed "$_am_raw" || return 0
  printf '%s\n' "$_am_raw"
}

agent_model() {
  _am_key="$1"
  [ -r "$AGENT_MODELS_FILE" ] || { agent_model_baseline; return 0; }
  _am_raw="$(sed 's/#.*//' "$AGENT_MODELS_FILE" 2>/dev/null \
             | grep -E "^[[:space:]]*$(printf '%s' "$_am_key" | sed 's/\./\\./g')[[:space:]]*=" \
             | tail -1 | cut -d= -f2- \
             | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  [ -n "$_am_raw" ] || { agent_model_baseline; return 0; }
  case "$_am_raw" in
    default|inherit|auto) agent_model_baseline; return 0 ;;
  esac
  _am_wellformed "$_am_raw" || {
    echo "[model] ignoring malformed model '$_am_raw' for $_am_key — using the baseline" >&2
    agent_model_baseline
    return 0
  }
  printf '%s\n' "$_am_raw"
}

# agent_model_from_label <label> — resolve a story's model label to a real id.
#
# Accepts a full id (`kimi/k3`) or a bare provider (`kimi`). A provider
# resolves to the first CONFIGURED model that provider offers, so the label
# does not have to be updated when a model version changes — and a provider
# with no credentials here resolves to nothing rather than to a run that dies
# at its first request.
#
# Prints nothing when it cannot resolve. The caller then keeps whatever it
# would have used, which is the safe direction: a story is still worked, just
# not on the model somebody hoped for.
agent_model_from_label() {
  _am_lbl="$(printf '%s' "${1:-}" | tr -d '[:space:]')"
  [ -n "$_am_lbl" ] || return 0
  case "$_am_lbl" in
    */*)
      # A full id: accept it only if openclaw actually has it configured.
      openclaw models list 2>/dev/null \
        | grep -E '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+[[:space:]]' \
        | grep -E '(^|[[:space:],])configured([[:space:],]|$)' \
        | awk '{print $1}' \
        | grep -ixF "$_am_lbl" | head -1
      ;;
    *)
      openclaw models list 2>/dev/null \
        | grep -E '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+[[:space:]]' \
        | grep -E '(^|[[:space:],])configured([[:space:],]|$)' \
        | awk '{print $1}' \
        | grep -i "^${_am_lbl}/" | head -1
      ;;
  esac
}

# agent_model_note <key> <model> — one line stating what this run is on.
#
# Always logged when a model resolved, baseline or not: "which model was this
# run on" is the first question asked when a run behaves oddly, and answering
# it from the log beats reconstructing it from the store afterwards.
agent_model_note() {
  [ -n "$2" ] || return 0
  if [ "$2" = "$(agent_model_baseline)" ]; then
    echo "[model] $1 running on $2 (environment baseline)"
  else
    echo "[model] $1 running on $2 (set in $AGENT_MODELS_FILE)"
  fi
}

# shellcheck shell=bash
# agent-thinking: how hard does each subsystem think?
#
# Sourced by the three runners — fixer, tester, reviewer — exactly like
# agent-limits.sh and agent-models.sh, and written by
# /usr/local/bin/agent-thinking, which the chat skills call.
#
# WHY A PER-SUBSYSTEM SETTING
# `openclaw agent --thinking <level>` is a per-run override, so the reviewer
# can reason hard while the tester does not — they are very different jobs and
# reasoning is not free. Without this the only control is `thinkingDefault` in
# the ConfigMap, which is one global value and needs a redeploy to change.
#
# WHAT THE LEVELS ACTUALLY DO, PER PROVIDER
# The scale openclaw accepts is off | minimal | low | medium | high. What
# reaches the provider is NOT the same everywhere, and pretending otherwise
# would be the misleading part:
#
#   kimi/k3          thinkingLevelMap collapses the five onto three:
#                    off -> nothing, minimal/low/medium -> low,
#                    high -> high, xhigh/max -> max.
#                    So `low` and `medium` are the same request.
#
#   minimax/*        the anthropic path sends {type: enabled, budget_tokens}.
#                    Effectively ON or OFF; the level does not currently
#                    change the budget. Treat anything but `off` as "on".
#
#   mistral/*        no reasoning declared at all — the flag is inert.
#
# Empty means "pass no --thinking", i.e. inherit thinkingDefault from the
# config. That is what an untouched deployment does, so this file cannot
# change behaviour until somebody sets something.
#
# FAIL-SOFT
# An unknown level is IGNORED rather than passed through. openclaw rejects a
# bad level and the whole turn dies; a run that thinks at the default is
# recoverable, a run that cannot start is not.

AGENT_THINKING_FILE="${AGENT_THINKING_FILE:-${HOME:-/home/node}/.openclaw/agent-thinking.conf}"

# The levels openclaw accepts. `default` is ours, meaning "send nothing".
AGENT_THINKING_LEVELS="off minimal low medium high"

_at_valid() {
  case " $AGENT_THINKING_LEVELS " in *" ${1:-} "*) return 0 ;; esac
  return 1
}

# agent_thinking <key> — the configured level, or empty to inherit.
agent_thinking() {
  _at_key="$1"
  [ -r "$AGENT_THINKING_FILE" ] || return 0
  _at_raw="$(sed 's/#.*//' "$AGENT_THINKING_FILE" 2>/dev/null \
             | grep -E "^[[:space:]]*$(printf '%s' "$_at_key" | sed 's/\./\\./g')[[:space:]]*=" \
             | tail -1 | cut -d= -f2- \
             | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr 'A-Z' 'a-z')"
  [ -n "$_at_raw" ] || return 0
  case "$_at_raw" in
    default|inherit|auto) return 0 ;;
  esac
  if ! _at_valid "$_at_raw"; then
    echo "[thinking] ignoring unknown level '$_at_raw' for $_at_key — inheriting the default" >&2
    return 0
  fi
  printf '%s\n' "$_at_raw"
}

# agent_thinking_note <key> <level> — one line, only when overridden.
agent_thinking_note() {
  [ -z "$2" ] || echo "[thinking] $1 thinking=$2 (from $AGENT_THINKING_FILE)"
}

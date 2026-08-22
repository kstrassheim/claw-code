# shellcheck shell=bash
# agent-limits: how long may each subsystem run?
#
# Sourced by the three runners — fixer, tester, reviewer — in the same pattern
# as project-kind and project-instructions, and written by
# /usr/local/bin/agent-limits, which the `developer`, `tester` and `reviewer`
# chat skills call.
#
# WHY A FILE AND NOT ENV
# ----------------------
# These caps were env-only: FIXER_MAX_LIFETIME, TESTER_AGENT_TIMEOUT,
# REVIEWER_AGENT_TIMEOUT — and AGENT_TURN_TIMEOUT was not even that, it was a
# literal in the script. Changing any of them meant editing the CI secret and
# rolling the pod, which is a deploy to adjust a number you often want to
# adjust *because* a run is going badly right now.
#
# The store lives on the workspace PVC, so a change survives restarts and
# redeploys, and it is read at the START of every run — the next spawned
# runner picks it up without anything being restarted.
#
# WHAT THE KEYS MEAN
# ------------------
#   solver.turn       one `openclaw agent` invocation of the issue solver
#   solver.lifetime   the solver's whole poll loop: several turns, waiting for
#                     new @-mention notes between them
#   tester.run        the tester's single agent run — it is one-shot, so this
#                     IS its lifetime
#   reviewer.run      likewise for the reviewer
#
# The solver has two because it is the only one that loops. Naming them apart
# matters: "3h" against a per-turn cap and against a whole-run cap are very
# different instructions, and the two were easy to confuse when both were
# called a timeout.

AGENT_LIMITS_FILE="${AGENT_LIMITS_FILE:-${HOME:-/home/node}/.openclaw/agent-limits.conf}"

# _al_parse <value> — human duration to seconds. Accepts a bare number of
# seconds, or a suffix: 90s, 45m, 3h, and combinations like 1h30m.
# Prints nothing and returns 1 if it cannot be understood, so a typo in the
# store can never be read as "0" (which would kill every agent instantly).
_al_parse() {
  _al_v="$(printf '%s' "${1:-}" | tr -d '[:space:]' | tr 'A-Z' 'a-z')"
  [ -n "$_al_v" ] || return 1
  case "$_al_v" in
    *[!0-9smh]*) return 1 ;;
  esac
  case "$_al_v" in
    *[0-9]) printf '%s\n' "$_al_v"; return 0 ;;   # bare seconds
  esac
  _al_total=0 _al_num=""
  while [ -n "$_al_v" ]; do
    _al_c="${_al_v%"${_al_v#?}"}"
    _al_v="${_al_v#?}"
    case "$_al_c" in
      [0-9]) _al_num="$_al_num$_al_c" ;;
      s) [ -n "$_al_num" ] || return 1; _al_total=$((_al_total + _al_num));        _al_num="" ;;
      m) [ -n "$_al_num" ] || return 1; _al_total=$((_al_total + _al_num * 60));   _al_num="" ;;
      h) [ -n "$_al_num" ] || return 1; _al_total=$((_al_total + _al_num * 3600)); _al_num="" ;;
      *) return 1 ;;
    esac
  done
  [ -z "$_al_num" ] || return 1
  [ "$_al_total" -gt 0 ] || return 1
  printf '%s\n' "$_al_total"
}

# _al_sane <seconds> — refuse values that would break a run rather than tune
# it. A cap under a minute kills every agent before it can think; one over a
# day outlives the locks and the planner's stale-lock TTL, so a wedged runner
# would hold its repo for a day.
AGENT_LIMIT_MIN=60
AGENT_LIMIT_MAX=86400
_al_sane() {
  [ "$1" -ge "$AGENT_LIMIT_MIN" ] && [ "$1" -le "$AGENT_LIMIT_MAX" ]
}

# agent_limit <key> <default-seconds> — the configured value, or the default.
#
# Falls back to the default on ANY problem: no file, no key, unparseable, out
# of range. A misconfigured limit must not stop the subsystem — it should run
# with the built-in value and leave the operator to notice, because the
# alternative is a bot that silently does nothing after a typo in chat.
agent_limit() {
  _al_key="$1"; _al_def="$2"
  [ -r "$AGENT_LIMITS_FILE" ] || { printf '%s\n' "$_al_def"; return 0; }
  _al_raw="$(sed 's/#.*//' "$AGENT_LIMITS_FILE" 2>/dev/null \
             | grep -E "^[[:space:]]*$(printf '%s' "$_al_key" | sed 's/\./\\./g')[[:space:]]*=" \
             | tail -1 | cut -d= -f2-)"
  [ -n "$_al_raw" ] || { printf '%s\n' "$_al_def"; return 0; }
  _al_sec="$(_al_parse "$_al_raw")" || { printf '%s\n' "$_al_def"; return 0; }
  _al_sane "$_al_sec" || { printf '%s\n' "$_al_def"; return 0; }
  printf '%s\n' "$_al_sec"
}

# agent_limit_is_set <key> — did a PERSON put a usable value in the store?
#
# `agent_limit` cannot answer this: it returns the default when the key is
# absent, so a caller cannot tell "nobody set it" from "somebody set it to
# exactly the default". The difference matters wherever a value can also be
# derived — a derived number may override a default, and must not override a
# human who deliberately set one.
#
# Usable, not merely present: a key holding a typo falls back to the default
# inside `agent_limit`, so reporting it as set would let an unreadable value
# suppress the derived one and leave the run on a number nobody chose.
agent_limit_is_set() {
  _al_key="$1"
  [ -r "$AGENT_LIMITS_FILE" ] || return 1
  _al_raw="$(sed 's/#.*//' "$AGENT_LIMITS_FILE" 2>/dev/null \
             | grep -E "^[[:space:]]*$(printf '%s' "$_al_key" | sed 's/\./\\./g')[[:space:]]*=" \
             | tail -1 | cut -d= -f2-)"
  [ -n "$_al_raw" ] || return 1
  _al_sec="$(_al_parse "$_al_raw")" || return 1
  _al_sane "$_al_sec"
}

# agent_count <key> <default> — a plain integer setting, NOT a duration.
#
# agent_limit() cannot serve this: it parses values as seconds and rejects
# anything under AGENT_LIMIT_MIN (60) as insane, so a story-point threshold of
# 3 would be silently replaced by its default and the setting would appear to
# work while being unchangeable. Different unit, different reader.
#
# 0 is VALID here and means off: the threshold doubles as the switch, so there
# is no second setting that can disagree with it.
agent_count() {
  _al_key="$1"; _al_def="$2"
  [ -r "$AGENT_LIMITS_FILE" ] || { printf '%s\n' "$_al_def"; return 0; }
  _al_raw="$(sed 's/#.*//' "$AGENT_LIMITS_FILE" 2>/dev/null \
             | grep -E "^[[:space:]]*$(printf '%s' "$_al_key" | sed 's/\./\\./g')[[:space:]]*=" \
             | tail -1 | cut -d= -f2- | tr -d '[:space:]' | tr 'A-Z' 'a-z')"
  [ -n "$_al_raw" ] || { printf '%s\n' "$_al_def"; return 0; }
  case "$_al_raw" in
    off|none|disabled|false) printf '0\n'; return 0 ;;
    *[!0-9]*)                printf '%s\n' "$_al_def"; return 0 ;;
  esac
  printf '%s\n' "$_al_raw"
}

# agent_flag <key> <default: on|off> — a switch.
#
# Anything unrecognised falls back to the default rather than being read as
# off: a typo must not silently disable a feature the operator believes is on.
agent_flag() {
  _al_key="$1"; _al_def="$2"
  [ -r "$AGENT_LIMITS_FILE" ] || { printf '%s\n' "$_al_def"; return 0; }
  _al_raw="$(sed 's/#.*//' "$AGENT_LIMITS_FILE" 2>/dev/null \
             | grep -E "^[[:space:]]*$(printf '%s' "$_al_key" | sed 's/\./\\./g')[[:space:]]*=" \
             | tail -1 | cut -d= -f2- | tr -d '[:space:]' | tr 'A-Z' 'a-z')"
  case "$_al_raw" in
    on|true|yes|enabled|auto) printf 'on\n' ;;
    off|false|no|disabled)    printf 'off\n' ;;
    *)                        printf '%s\n' "$_al_def" ;;
  esac
}

# agent_limit_note <key> <default> <effective> — one log line, only when the
# store actually changed something. A run that uses the built-in value should
# not add noise; a run that does not is worth being able to see afterwards in
# the log, when someone asks why it stopped when it did.
agent_limit_note() {
  [ "$2" = "$3" ] || echo "[limits] $1 = ${3}s (default ${2}s, from $AGENT_LIMITS_FILE)"
}

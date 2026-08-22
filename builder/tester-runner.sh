#!/bin/bash
# tester-runner: backgrounded subprocess inside the openclaw container.
# Per-repo, per-commit website + pipeline tester that runs in parallel
# to the fixer-runner. Different role: it READS and TESTS, never writes
# code or opens PRs.
#
# Per cron tick (see k8s/051-tester.yaml):
#   1. Refuse outright unless the repository is on the allowed-projects
#      list (`project-allow check`) — being a collaborator is how someone
#      ASKS to be tested; the list is the answer.
#   2. Resolve the bot identity from $GITHUB_TOKEN (same trick as
#      fixer-runner).
#   3. Compare GitHub's current main HEAD for this repo against the
#      saved last-tested HEAD at $STATE_ROOT/tester-state/<repo>.last-head.
#      Same → exit silently (no chat post per spec).
#      Different → proceed.
#   4. Acquire a per-repo tester lock so two tester subprocesses
#      can't race on the same repo (same mkdir-atomic pattern as the
#      fixer's lock, but a separate dir so the two subsystems don't
#      block each other).
#   5. Clone/update the repo into $STATE_ROOT/tester-projects/<repo>/
#      (separate from $STATE_ROOT/projects/, which the fixer uses).
#   6. Take a slot in the shared model-concurrency gate — at LOW priority,
#      so a tester run (the longest of the three by a wide margin) can
#      never start ahead of the issue solver or the reviewer.
#   7. Run `openclaw agent --local` with the TESTER prompt — distinct
#      from the fixer prompt. The agent only stages issue drafts as
#      JSON files in $DRAFTS_DIR; it does NOT create issues itself.
#   8. After agent exits, the wrapper reads drafts, drops the ones that
#      duplicate an already-open issue, and creates the GitHub issues,
#      substituting the right assignee (BOT for things the issue-solver
#      can fix; OWNER for things needing the human).
#   9. Mark the HEAD as tested, post a short result summary, exit.
#
# THE THREE OPTIONAL SCAN PASSES
# On top of the functional test the run can do three more passes. All
# three are OFF BY DEFAULT and each is a flag file on the workspace
# volume, toggled from chat (see the `tester` skill in
# k8s/051-tester.yaml). A fresh pod therefore does the functional test
# and nothing else — every extra pass is something a human deliberately
# switched on:
#
#   SAST            $STATE_ROOT/.sast-enabled        `tester sast on`
#     Static scan of the WHOLE tree at the tested commit: semgrep,
#     bandit, pip-audit, npm audit, PSScriptAnalyzer, and gitleaks over
#     BOTH the working tree and the git history. Needs no per-repository
#     authorisation — it only reads the project's own source.
#
#   pen test (DAST) $STATE_ROOT/.pentest-enabled     `tester pentest on`
#     Live scanning of the DEPLOYED app through the `pentest` MCP.
#     TRIPLE-GATED, because actively scanning a host you were not
#     authorised to scan can be illegal:
#       (a) the deploy checks for this commit succeeded,
#       (b) the target repository ships a `PENTEST_ALLOWED_HOSTS` file in
#           its root naming its own host(s), and
#       (c) this chat switch is on.
#     Any one missing and the scan is skipped, with the reason stated in
#     the log, in the agent's prompt and in the run report.
#     `PENTEST_ALLOWED_HOSTS` is a HUMAN-ONLY file: the wrapper sets the
#     MCP's host allowlist from THAT FILE AND NOTHING ELSE — not from
#     the environment, not from the chat switch — so switching the pen
#     test on can never authorise a repository that did not opt in.
#
#   AI code review  $STATE_ROOT/.codereview-enabled  `tester codereview on`
#     The agent reads the whole repository and reasons about it, for
#     security a ruleset cannot find (broken authorization, IDOR, missing
#     server-side validation, privilege escalation, tenant leakage,
#     secrets in logs or bundles, SSRF, races, business-logic flaws) AND
#     for ordinary quality (correctness, API/data integrity, N+1 and
#     unbounded queries, maintainability). It runs after the other two
#     and does NOT depend on them. Off by default because it reads the
#     whole repository — the slowest and most expensive pass.
#
# Findings from every pass are consolidated into ONE issue per ROOT
# CAUSE before anything is filed, and the wrapper drops any draft that
# duplicates an already-open issue.
#
# Args:
#   $1 repo full_name   (owner/name)
#
# Required env:
#   GITHUB_TOKEN        bot PAT (already on the openclaw pod)
#   ENTRA_*             only if the deployed site is Entra-protected
#                       (the agent reads these via TOOLS-entra.md)
#
# Optional env:
#   TESTER_BOT_LOGIN    bot's GH login. If unset, resolved at startup.
#   TESTER_MAX_LIFETIME overall wall-clock cap, seconds (default 3600).
set -uo pipefail

REPO="${1:?repo full_name required (owner/name)}"

# Permission gate — see builder/project_allowlist.py. The planner filters too;
# this is the check that also covers a hand-started run, and it sits ahead of
# every API call and the checkout so a refusal costs nothing and leaves no
# state. Exit 2 is the CLI's "not permitted"; anything else means the list
# could not be read, which permits nothing either. Exits 0: refusing is the
# designed outcome, not a failure.
if ! PERM_REASON="$(project-allow check "$REPO" 2>&1)"; then
  echo "[permission] refusing to test $REPO — ${PERM_REASON:-project-allow unavailable}" >&2
  echo "[permission] Grant it from chat with:  projects add $REPO" >&2
  exit 0
fi

# The shared shell libraries. Installed without the .sh suffix in the image;
# the suffix is tried too so this runner also works in a tree where they have
# not been renamed yet. A library that is genuinely absent is not fatal: each
# call site below has a plain fallback, because a missing tuning knob must not
# stop a test run from happening.
# Where the sourced helper libraries live. Next to this script when running
# from a checkout, /usr/local/bin in the image. Probed on a file that only
# ever ships as a library, so a directory holding just the CLIs is rejected.
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
[ -n "$LIB_DIR" ] && [ -r "$LIB_DIR/agent-models.sh" ] || LIB_DIR="/usr/local/bin"
_source_lib() {
  # ONLY EVER SOURCE THE .sh FILE.
  #
  # Each runtime knob ships as a PAIR in the same directory: `agent-limits` is
  # the CLI a human runs, `agent-limits.sh` is the library this sources. An
  # earlier version searched the bare name first and therefore found the CLI.
  #
  # Sourcing the CLI does not merely fail to define the helpers. The CLI parses
  # its (absent) subcommand, prints `unknown command`, and calls `exit` — and
  # in a SOURCED file that exit belongs to the caller, so the runner died on
  # its second line having done nothing. The symptom carried no error: the
  # spawner reported a successful spawn every tick while no run ever started,
  # no lock was taken and no log was written.
  #
  # The suffix is the whole rule. Testing the execute bit instead was tried and
  # rejected: some filesystems force 0755 on every file, and there the guard
  # refuses the real library and silently drops every knob back to its default.
  # A name is a fact about the file; a mode bit is a fact about the volume.
  for _d in "$LIB_DIR" /usr/local/bin; do
    if [ -r "$_d/$1.sh" ]; then . "$_d/$1.sh"; return 0; fi
  done
  return 1
}
_source_lib agent-limits || true
_source_lib agent-models || true
_source_lib agent-thinking || true
_source_lib agent-slot || true
_source_lib project-instructions || true
_source_lib project-kind || true

# Resolve bot identity. Pinned identity (env var) wins; otherwise look it
# up. Hardcoding would couple the code to one deployment's identity —
# different clusters use different bot accounts.
if [ -n "${TESTER_BOT_LOGIN:-}" ]; then
  BOT_LOGIN="$TESTER_BOT_LOGIN"
else
  BOT_LOGIN="$(forge-cli --repo "$REPO" identity 2>/dev/null)"
  if [ -z "$BOT_LOGIN" ]; then
    echo "FATAL: tester cannot resolve bot identity from \$GITHUB_TOKEN /user" >&2
    exit 1
  fi
fi

# Repo owner — used as the OWNER @-mention / assignee target for issues
# the bot can't act on (e.g. Entra access denied). Derived from $REPO
# so no API call needed; identity-agnostic.
REPO_OWNER="${REPO%%/*}"

MAX_LIFETIME_SECONDS="${TESTER_MAX_LIFETIME:-3600}"
# Runtime-adjustable via `agent-limits` (stored on the PVC, read here at the
# start of the run). The tester is one-shot, so this cap IS its turn budget.
_TESTER_RUN_DEFAULT="${TESTER_AGENT_TIMEOUT:-3000}"
if command -v agent_limit >/dev/null 2>&1; then
  AGENT_TURN_TIMEOUT="$(agent_limit tester.run "$_TESTER_RUN_DEFAULT")"
  agent_limit_note tester.run "$_TESTER_RUN_DEFAULT" "$AGENT_TURN_TIMEOUT" 2>/dev/null || true
else
  AGENT_TURN_TIMEOUT="$_TESTER_RUN_DEFAULT"
fi

# Which model this run uses, and how hard it thinks. Empty means inherit the
# config default, which is what an untouched deployment produces — see
# builder/agent-models.sh and builder/agent-thinking.sh.
AGENT_MODEL_ARGS=()
if command -v agent_model >/dev/null 2>&1; then
  AGENT_MODEL="$(agent_model tester)"
  agent_model_note tester "$AGENT_MODEL" 2>/dev/null || true
  [ -z "$AGENT_MODEL" ] || AGENT_MODEL_ARGS=(--model "$AGENT_MODEL")
fi
if command -v agent_thinking >/dev/null 2>&1; then
  AGENT_THINKING="$(agent_thinking tester)"
  agent_thinking_note tester "$AGENT_THINKING" 2>/dev/null || true
  [ -z "$AGENT_THINKING" ] || AGENT_MODEL_ARGS+=(--thinking "$AGENT_THINKING")
fi

STATE_ROOT="${HOME:-/home/node}/.openclaw"
TESTER_PROJECTS_ROOT="$STATE_ROOT/tester-projects"
PROJECT_DIR="$TESTER_PROJECTS_ROOT/$REPO"
LOCK_ROOT="$STATE_ROOT/.tester-locks"
LOCK_DIR="$LOCK_ROOT/${REPO//\//__}"
LOG_DIR="$STATE_ROOT/tester-logs"
LOG_FILE="$LOG_DIR/${REPO//\//_}.log"
TESTER_STATE_DIR="$STATE_ROOT/tester-state"
LAST_HEAD_FILE="$TESTER_STATE_DIR/${REPO//\//__}.last-head"
TESTER_DRAFTS_ROOT="$STATE_ROOT/tester-drafts"
SUMMARIES_DIR="$STATE_ROOT/tester-summaries"

mkdir -p "$LOG_DIR" "$LOCK_ROOT" "$TESTER_STATE_DIR" "$TESTER_DRAFTS_ROOT" \
         "$SUMMARIES_DIR" "$(dirname "$PROJECT_DIR")"

# Per-repo lock (sibling of fixer's $STATE_ROOT/.fixer-locks/ — the two
# subsystems hold independent locks so they never block each other).
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date -Iseconds)] tester lock held for $REPO; aborting" >> "$LOG_FILE"
  exit 0
fi
echo "$BASHPID $(date -Iseconds)" > "$LOCK_DIR/owner"

# Cleanup on every exit path: release the shared model slot, drop the lock
# and kill any watcher subshells we spawned so they don't leak into the pod.
# The slot goes first: the tester's run is the longest of the three, so a slot
# it leaked would throttle the solver and the reviewer for a long time.
SENTINEL_WATCHER_PID=""
LIFETIME_WATCHER_PID=""
cleanup() {
  command -v release_agent_slot >/dev/null 2>&1 && release_agent_slot
  [ -n "$SENTINEL_WATCHER_PID" ] && kill "$SENTINEL_WATCHER_PID" 2>/dev/null
  [ -n "$LIFETIME_WATCHER_PID" ] && kill "$LIFETIME_WATCHER_PID" 2>/dev/null
  rm -rf "$LOCK_DIR"
}
trap cleanup EXIT

exec >> "$LOG_FILE" 2>&1

echo "============================================================"
echo "[$(date -Iseconds)] tester start  repo=$REPO"
echo "============================================================"

# ---- GH API helpers ------------------------------------------------

# Every question this runner asks its host goes through `forge-cli` — the same
# implementation the planners import — so nothing below knows a URL or an auth
# header, and the tester works against a project on either host.
FORGE=(forge-cli --repo "$REPO")

# The commit under test: the head of the repository's DEFAULT branch.
#
# Asked as one question now. It used to be three requests and a comment
# apologising for them — fetch `main`, and if that 404s fetch the repository to
# learn the real default branch name, then fetch that branch — with the branch
# name briefly living in the variable meant for the sha. Which branch is the
# default is the host's business, and it answers both halves at once.
DEFAULT_BRANCH=""
HEAD_SHA=""
if _default_json="$("${FORGE[@]}" default-branch 2>/dev/null)"; then
  { read -r DEFAULT_BRANCH; read -r HEAD_SHA; } < <(printf '%s' "$_default_json" \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('branch') or '')
print(d.get('sha') or '')
" 2>/dev/null)
fi
: "${DEFAULT_BRANCH:=main}"

if [ -z "$HEAD_SHA" ]; then
  echo "[tester] could not fetch HEAD for $REPO — exit"
  exit 0
fi

# Already-tested gate: bail if HEAD hasn't changed since the last run.
LAST_HEAD=""
[ -f "$LAST_HEAD_FILE" ] && LAST_HEAD="$(cat "$LAST_HEAD_FILE")"
if [ "$HEAD_SHA" = "$LAST_HEAD" ]; then
  echo "[tester] HEAD $HEAD_SHA on $DEFAULT_BRANCH already tested — exiting silently"
  exit 0
fi
echo "[tester] new HEAD: '$LAST_HEAD' → '$HEAD_SHA' on branch $DEFAULT_BRANCH"

# ---- scan switches -------------------------------------------------
# Three independent passes, each a flag file on the workspace volume and each
# OFF BY DEFAULT. The flag's PRESENCE means enabled, so "no flags on a fresh
# pod" is the quiet, cheap configuration — the state a deployment nobody has
# touched is in, rather than one that starts scanning everything it can reach.
#
# Toggled from chat (`tester sast|pentest|codereview on|off|status`), never
# from an env var: an env var needs a secret edit and a rollout to change, and
# a switch that needs a rollout is a switch nobody flips.
#
# Read here, BEFORE the checkout, because the checkout depends on them: the
# static scan reads the git HISTORY, which a shallow clone does not have.
SAST_ENABLE_FLAG="$STATE_ROOT/.sast-enabled"
PENTEST_ENABLE_FLAG="$STATE_ROOT/.pentest-enabled"
CODEREVIEW_ENABLE_FLAG="$STATE_ROOT/.codereview-enabled"
SAST_ON=0;    [ -f "$SAST_ENABLE_FLAG" ]       && SAST_ON=1
PENTEST_ON=0; [ -f "$PENTEST_ENABLE_FLAG" ]    && PENTEST_ON=1
CR_ON=0;      [ -f "$CODEREVIEW_ENABLE_FLAG" ] && CR_ON=1
_onoff() { if [ "$1" = 1 ]; then echo ON; else echo OFF; fi; }
echo "[scan-switches] SAST=$(_onoff "$SAST_ON")  pen-test=$(_onoff "$PENTEST_ON")  AI-code-review=$(_onoff "$CR_ON")  (all default OFF)"

# ---- workspace setup ----------------------------------------------

if [ ! -d "$PROJECT_DIR/.git" ]; then
  echo "[tester] cloning $REPO into $PROJECT_DIR (shallow)"
  if ! git clone --quiet --depth 50 "https://github.com/$REPO.git" "$PROJECT_DIR"; then
    echo "[tester] clone failed — exit"
    exit 0
  fi
fi
cd "$PROJECT_DIR"
git fetch --quiet origin "$DEFAULT_BRANCH" --depth 50 2>/dev/null || git fetch --quiet origin "$DEFAULT_BRANCH"
git checkout --quiet --force "$DEFAULT_BRANCH" 2>/dev/null || git checkout --quiet -b "$DEFAULT_BRANCH" "origin/$DEFAULT_BRANCH"
git reset --hard --quiet "origin/$DEFAULT_BRANCH"
git clean -fdx --quiet

# gitleaks over the HISTORY needs the history, and a shallow clone does not
# have it: a credential that was committed and later "removed" would look
# absent, which is the one answer that scan must never give wrongly. Deepen
# only when the static scan is actually switched on — an unshallow costs real
# time on a large repository and no other pass needs it.
if [ "$SAST_ON" = "1" ] && [ -f "$PROJECT_DIR/.git/shallow" ]; then
  echo "[tester] SAST is on — unshallowing the checkout so the history scan has history"
  git fetch --quiet --unshallow 2>/dev/null \
    || echo "[tester] note: could not unshallow (history scan will see only the shallow window)"
fi
SOURCE_READY=0
[ -d "$PROJECT_DIR/.git" ] && SOURCE_READY=1

# Per-tester drafts dir for THIS commit. Wrapper reads $DRAFTS_DIR/*.json
# after the agent exits and turns each into a GitHub issue.
DRAFTS_DIR="$TESTER_DRAFTS_ROOT/${REPO//\//__}-$HEAD_SHA"
rm -rf "$DRAFTS_DIR"
mkdir -p "$DRAFTS_DIR"

# Per-tester isolation so parallel testers across repos don't fight
# over the same browser profile or az CLI config. Browser plugin
# reads $BROWSER_PROFILE; az reads $AZURE_CONFIG_DIR; kubectl reads
# $KUBECONFIG.
export TESTER_REPO="$REPO"
export TESTER_HEAD_SHA="$HEAD_SHA"
export TESTER_DRAFTS_DIR="$DRAFTS_DIR"
export TESTER_BOT_LOGIN_VAL="$BOT_LOGIN"
export TESTER_REPO_OWNER="$REPO_OWNER"
export BROWSER_PROFILE="tester-${REPO//\//_}"
export AZURE_CONFIG_DIR="$STATE_ROOT/tester-azure/${REPO//\//__}"
mkdir -p "$AZURE_CONFIG_DIR"

SESSION_ID="tester-${REPO//\//-}-$(date +%s)"

# ---- deploy checks -------------------------------------------------
# Did THIS commit's CI actually go green? The agent inspects the workflow runs
# itself in PHASE 1 (that is where CI failures become issues); this is the
# wrapper's own, coarser read, because one decision cannot be left to the
# agent's judgement: whether a LIVE SECURITY SCAN may run at all. Scanning the
# deployment of a commit whose deploy failed means scanning whatever was live
# before it, and reporting the result against a commit that never shipped.
#
# States: green | failed | pending | none. Only `green` opens the gate.
# `none` is deliberately NOT green: a repository with no checks on this commit
# has told us nothing about whether it deployed.
# The reduction is the forge's, and this is the only place the tester asks.
# It used to carry its own copy — a third one, subtly different from the other
# two: it called a passing run `success` where everything else says `green`,
# and any incomplete run made the whole commit `pending` even when another had
# already failed. One vocabulary, one answer.
DEPLOY_CHECKS="$("${FORGE[@]}" checks --sha "$HEAD_SHA" 2>/dev/null || echo pending)"
[ -n "$DEPLOY_CHECKS" ] || DEPLOY_CHECKS="pending"
echo "[deploy-checks] $HEAD_SHA: $DEPLOY_CHECKS"

# ---- pentest authorisation gate ------------------------------------
# Actively scanning a host nobody authorised you to scan can be ILLEGAL, so
# the live scan runs only when ALL THREE of these hold, and is skipped with a
# stated reason otherwise ("if it isn't sure, it doesn't scan"):
#
#   (a) the deploy checks for this commit SUCCEEDED — there is a deployment of
#       THIS commit to scan;
#   (b) the target repository AUTHORISED it, by committing a
#       `PENTEST_ALLOWED_HOSTS` file to its root naming its own host(s), one
#       host or URL per line, `#` comments allowed;
#   (c) the pen test is SWITCHED ON here (`tester pentest on`).
#
# The order below is cheapest-first, and the FIRST failing gate is the one
# reported: a switched-off pen test costs no API call to establish.
#
# PENTEST_ALLOWED_HOSTS is a HUMAN-ONLY authorisation. The MCP's allowlist is
# set from THAT FILE AND NOTHING ELSE — it is reset to empty here first, so a
# value inherited from the pod environment cannot survive into the scan, and
# the chat switch alone can never authorise a repository that did not opt in.
# The `pentest` MCP is fail-closed and enforces the list in code, so even a
# misfired tool call cannot reach a host that is not on it.
export PENTEST_ALLOWED_HOSTS=""
PENTEST_ACTIVE=0
PENTEST_SKIP_REASON=""
if [ "$PENTEST_ON" != "1" ]; then
  PENTEST_SKIP_REASON="the pen test is switched off (default) — enable it from chat with \`tester pentest on\`"
elif [ "$DEPLOY_CHECKS" != "green" ]; then
  PENTEST_SKIP_REASON="the deploy checks for $HEAD_SHA did not succeed (state: $DEPLOY_CHECKS) — there is no verified deployment of this commit to scan"
else
  # Read the authorisation file from the repository ROOT at the COMMIT UNDER
  # TEST, over the API rather than out of the checkout: the file has to be the
  # one the owner committed to the tested commit, not whatever happens to be
  # in a working tree the agent may have touched.
  _pentest_hosts_raw="$("${FORGE[@]}" file-at-ref \
    --path PENTEST_ALLOWED_HOSTS --ref "$HEAD_SHA" 2>/dev/null || true)"
  _pentest_hosts=""
  if [ -n "$_pentest_hosts_raw" ]; then
    _pentest_hosts="$(printf '%s' "$_pentest_hosts_raw" | python3 -c "
import sys, urllib.parse
hosts = []
for line in sys.stdin.read().splitlines():
    line = line.split('#', 1)[0].strip()
    if not line:
        continue
    h = urllib.parse.urlparse(line).hostname if '://' in line else line.split('/')[0].split(':')[0]
    h = (h or '').strip().lower()
    if h and h not in hosts:
        hosts.append(h)
print(','.join(hosts))
" 2>/dev/null || echo "")"
  fi
  if [ -z "$_pentest_hosts" ]; then
    PENTEST_SKIP_REASON="$REPO has not authorised live scanning — no usable \`PENTEST_ALLOWED_HOSTS\` file in the repository root at $HEAD_SHA"
  else
    export PENTEST_ALLOWED_HOSTS="$_pentest_hosts"
    PENTEST_ACTIVE=1
  fi
fi
if [ "$PENTEST_ACTIVE" = "1" ]; then
  echo "[pentest] authorised for $REPO — hosts: $PENTEST_ALLOWED_HOSTS"
else
  echo "[pentest] skipped — $PENTEST_SKIP_REASON"
fi

# ---- scan prompt sections ------------------------------------------
# One variable per optional pass, composed from the switches above and spliced
# into the prompt between PHASE 4 and PHASE 9. A pass that did not run is
# described to the agent as explicitly as one that did: "skipped" and "clean"
# must never be confusable in the report.

if [ "$SAST_ON" != "1" ]; then
  SAST_STEP="## PHASE 5 — static security scan (SAST): SWITCHED OFF

The static scan is off unless a human turns it on (\`tester sast on\`).
Do NOT run semgrep or the \`security\` MCP tools this run. Report SAST as
**skipped** in your summary and carry on."
elif [ "$SOURCE_READY" != "1" ]; then
  SAST_STEP="## PHASE 5 — static security scan (SAST): UNAVAILABLE

The source checkout for $HEAD_SHA could not be prepared, so the static
scan could not run. Report SAST as **inconclusive** in your summary —
NOT clean and NOT skipped: nothing was examined. Carry on."
else
  SAST_STEP="## PHASE 5 — static security scan (SAST): the WHOLE tree

Audit the SOURCE of the exact commit under test. A read-only checkout is
already prepared for you at:

    $PROJECT_DIR   (on $DEFAULT_BRANCH at $HEAD_SHA)

Work there — never commit, push, branch or modify anything in it.
Scan the FULL project, not a diff: the pull-request reviewer already
covers what an individual change introduced, so your job here is the
standing posture of the codebase as shipped, including long-standing
problems no single pull request would surface again.

Run ALL of the applicable tools, pointing each at that checkout (the
parameter differs per tool — check each tool's schema):
  - \`semgrep\` MCP over the repository (security rulesets — the OWASP /
    security-audit style rules, plus language rules for what's present).
  - \`security\` MCP, each where the language applies:
      \`bandit_scan\` \`path=$PROJECT_DIR\` (Python),
      \`pip_audit\` \`requirements=<each requirements file>\` (Python
      dependency CVEs — repeat per file),
      \`npm_audit\` \`cwd=<each dir containing package.json>\` (JS
      dependency CVEs — repeat per package),
      \`psscriptanalyzer_scan\` \`path=$PROJECT_DIR\` (PowerShell),
      \`gitleaks_scan\` \`path=$PROJECT_DIR\` (secrets in the WORKING TREE),
      \`gitleaks_git_scan\` \`path=$PROJECT_DIR\` (secrets in the git
      HISTORY — a credential that was committed and later 'removed' is
      still recoverable, so history is a separate scan and not optional).
      A real leaked credential is ALWAYS worth an issue, and the fix is to
      ROTATE it; rewriting history alone does not un-leak it.

**Triage hard.** Report only findings that are REAL and RELEVANT to this
codebase. A dependency CVE counts only if the vulnerable package is
actually reachable in this app. Drop generic linter noise, test fixtures
and rules that plainly do not apply — a wall of low-confidence findings
trains everyone to ignore the scanner.

**Stage NOTHING yet.** Carry what survives triage to PHASE 8, which
consolidates the findings of every phase before any draft is written.
Note each finding's root cause and the fix it needs, so PHASE 8 can group
them: the same unsafe pattern in five files is ONE finding with five
locations."
fi

if [ "$PENTEST_ACTIVE" != "1" ]; then
  SECURITY_STEP="## PHASE 6 — live security scan (pen test): SKIPPED

Reason: $PENTEST_SKIP_REASON.

Do NOT run any nuclei/testssl scan and do NOT scan any host this run.
Report the pen test as **skipped**, with that reason, in your summary.
Continue with the remaining phases."
else
  SECURITY_STEP="## PHASE 6 — live security scan (pen test): AUTHORISED

You reach this phase only because the deploy checks for this commit
SUCCEEDED, the repository authorised live scanning in its
\`PENTEST_ALLOWED_HOSTS\` file, and the scan is switched on here.

Scan the RUNNING site — not the source — with the \`pentest\` MCP.
**Scan ONLY these host(s)**, which the repository itself authorised;
nothing else, ever:

    $PENTEST_ALLOWED_HOSTS

The \`pentest\` MCP is hard-locked to those host(s): any other target
(production, GitHub, third parties, internal IPs) is refused in code.

**ORDER MATTERS — start BOTH scans FIRST, before further browser work.**
Both are asynchronous: each returns a \`jobId\` immediately and keeps
working in the BACKGROUND, so they overlap with testing you are doing
anyway. Do NOT wait on either, and do NOT re-run a scan already running.
  a. Start both now:
       \`pentest.nuclei_scan url=https://<authorised-host>\` — template
       DAST for CVEs, exposed panels/config files, default credentials,
       missing or weak headers, technology exposure (~30 min, deliberately
       rate-limited so the host does not throttle us).
       \`pentest.testssl_scan target=<authorised-host>\` — TLS posture:
       weak protocols/ciphers, cert chain and expiry, known TLS flaws
       (a few minutes).
     Note both jobIds and get on with the rest of the run.
  b. At the END of the run, before writing your summary, collect each with
     \`pentest.scan_status jobId=<id>\`. If one still says \`running\`,
     finish other work and ask again.

**Never report a scan as clean unless its job actually reached \`done\`.**
A scan still running, timed out or errored is **inconclusive** — report it
with that exact word, per scanner. \`skipped\` means you deliberately did
not run it; an unfinished scan means the check silently did not happen. A
false all-clear is worse than an honest \"no result\".

These DETECT, they do not exploit. Triage the output: only REAL, confirmed
problems on THIS deployment. Where a finding is visually demonstrable (an
exposed admin page, a directory listing), open it in the browser and take a
screenshot as evidence. Carry the findings to PHASE 8 — stage nothing here."
fi

if [ "$CR_ON" != "1" ]; then
  CODEREVIEW_STEP="## PHASE 7 — AI code review: SWITCHED OFF

The whole-repository review is off unless a human turns it on
(\`tester codereview on\`). Skip it and report it as **skipped** in your
summary."
elif [ "$SOURCE_READY" != "1" ]; then
  CODEREVIEW_STEP="## PHASE 7 — AI code review: UNAVAILABLE

The source checkout for $HEAD_SHA could not be prepared, so the review
could not run. Report it as **inconclusive** in your summary — NOT clean,
NOT skipped."
else
  CODEREVIEW_STEP="## PHASE 7 — AI code review: read the WHOLE project yourself

Same read-only checkout as PHASE 5:

    $PROJECT_DIR   (on $DEFAULT_BRANCH at $HEAD_SHA)

This is a FULL review — **security AND general code quality, not one or
the other**. PHASES 5 and 6 were pattern matching and probing; this phase
is YOU reading the code and reasoning about it, which is the only way to
catch:
  - **Security by reasoning, not signature:** broken authentication or
    authorization (an endpoint that never checks the caller owns the
    record), IDOR, missing server-side validation behind a validating UI,
    privilege escalation, tenant or data leakage between users, secrets
    and tokens reaching logs or the client bundle, unsafe
    deserialisation, SSRF, race conditions on shared state, and
    business-logic flaws (a negative quantity, a re-submitted request
    that double-charges). No ruleset finds these — they need
    understanding of what the code is FOR.
  - **Correctness and robustness:** unhandled failure paths, swallowed
    exceptions, missing transaction boundaries, wrong edge-case handling,
    concurrency bugs, resource leaks.
  - **API and data integrity:** endpoints whose behaviour contradicts
    their contract or the frontend's expectation, validation that differs
    between layers, migrations that can lose data.
  - **Performance traps:** N+1 queries, unbounded result sets, work done
    per-request that should be cached or batched.
  - **Maintainability that will cause the NEXT bug:** logic duplicated
    where it must be changed in several places, dead or misleading code,
    error handling that hides the cause, missing tests around genuinely
    risky logic.

Prioritise by what the app actually exposes: start at the entry points
(HTTP routes, auth middleware, database access, anything handling user
input or money), then follow the data. You do NOT need to read every
file — read what matters and say in the summary what you covered.

**Do not depend on PHASES 5 and 6 having run.** Both are off by default
and may have been skipped or inconclusive this run — in that case this is
the ONLY look anyone takes at this code, so do the security reasoning
yourself and never defer a concern to a scan that did not happen. If they
DID run and already reported a finding, don't repeat it; add to it only
when you can explain an exploit path or consequence they could not see.

Carry your findings to PHASE 8 — stage nothing here. Be strict about
signal: a handful of real, well-argued findings is the goal. Naming,
formatting and personal-preference refactors are NOT findings."
fi

CONSOLIDATE_STEP="## PHASE 8 — consolidate, THEN stage the drafts

**Consolidate across ALL phases BEFORE writing any draft.** Collect every
finding — the functional ones you already staged in PHASE 4, plus what
PHASES 5, 6 and 7 produced — into one list and group them by ROOT CAUSE
AND FIX, not by which tool reported them.

The same weakness routinely shows up in several phases wearing different
clothes: the static scan flags a missing header in the code, the live scan
sees it missing on the response, and the code review notices the
middleware was never registered. That is ONE issue with three pieces of
evidence, not three issues. Likewise the same unsafe pattern repeated
across five files is ONE issue listing five locations. Ask yourself: would
one change close both? If yes, it is one issue.

Rewrite the drafts to match that grouping. You staged files in PHASE 4;
if two of them share a root cause, delete both and write one draft that
carries the evidence of each (\`rm\` the superseded files from
$DRAFTS_DIR — a draft left behind becomes a duplicate issue). Name the
consolidated drafts \`08-finding-<n>.json\`.

For each consolidated finding the draft body states: the single root
cause, the single fix, every observation under it (say which phase saw
what — that is exactly the evidence a fixer needs), repro steps, expected
vs actual, and the tested commit $HEAD_SHA. Cite \`file:line\` where the
finding is in the source. State the concrete consequence: for security,
who can do what to whom; otherwise what breaks, for whom, and when. If you
cannot describe a consequence, it is an observation, not a finding —
leave it out.

**Title a security finding with the prefix \`🔒 Security:\`.** Everything
else — correctness, integrity, performance, maintainability — gets an
ordinary title with no prefix.

**Assignment** (\`assigneeRole\`, per the rules at the top of this prompt):
  - \"BOT\" when the fix is in the app's OWN CODE and the issue-solver can
    make it: a functional bug, a missing header the app itself sets, an
    endpoint missing an authorization check, an unsafe query, a dependency
    bump.
  - \"OWNER\" when it needs a human: infrastructure, TLS/WAF or DNS
    configuration, credentials to rotate or grant, access the bot was
    denied, or a product decision. Most live-infrastructure security
    findings are OWNER.
Every draft gets exactly one of the two. There is no third option.

You do NOT need to check for duplicates yourself — the wrapper compares
every draft against the repository's currently OPEN issues before it files
anything, and drops the ones that report a problem already tracked."

# ---- TESTER prompt ------------------------------------------------
# Hard-quoted heredocs so no $variable expansion: every dynamic value
# is substituted by sed below. Avoids the .32/.36/.43 quote-escape
# bugs that bit the fixer-runner prompt. The optional-pass sections
# built above are spliced in between the two halves.
read -r -d '' PROMPT_HEAD <<'PROMPT_HEAD_EOF' || true
You are working autonomously as the TESTER for repository __REPO__
at commit `__HEAD_SHA__` on branch `__DEFAULT_BRANCH__`.

## YOUR ROLE — read this first

You TEST deployed software. You DO NOT:
  - edit code
  - run `git add`/`commit`/`push`
  - open pull requests
  - look at existing issues or open PRs
  - try to "fix" anything you find

You DO:
  - inspect CI workflow runs via the github MCP
  - drive the browser plugin to test the deployed site
  - use `az`/`kubectl`/cloud CLIs to fetch logs for error context
  - run whichever of the optional scan phases below are switched on
  - stage issue drafts as JSON files in __DRAFTS_DIR__
  - emit a brief summary on stdout when done

The wrapper that spawned you will read __DRAFTS_DIR__/*.json AFTER
you exit and create GitHub issues from them. It substitutes
assignees per the rules below — do NOT include real logins.

## Issue draft format

One JSON file per finding, in __DRAFTS_DIR__, named like
`01-pipeline.json`, `03-unreachable.json`, `04-error-1.json`,
`08-finding-1.json`, etc.

Schema:
```json
{
  "title": "concise title (1 line)",
  "body": "markdown body with concrete details and log excerpts. Do NOT reference screenshots in the body text — the wrapper appends a Screenshots section automatically.",
  "assigneeRole": "BOT" | "OWNER",
  "media": [
    {
      "path": "/home/node/.openclaw/media/browser/<uuid>.png",
      "alt":  "what this screenshot shows in 5-10 words"
    }
  ]
}
```

About `media`:
  - Optional; omit or set to `[]` if there are no screenshots.
  - Each `path` must be an absolute path that the browser tool
    actually produced this run (it returns these as the
    `mediaPath` field of the screenshot result, and emits them as
    `MEDIA:<path>` lines in the agent log).
  - The wrapper uploads each PNG to an orphan branch
    `tester-screenshots` in the same repo and appends a
    `## Screenshots` section to the body with `![alt](raw-url)` —
    so do not try to embed images yourself.

Use **BOT** when the issue is something the issue-solver in this
same pod CAN fix on the next iteration:
  - pipeline failure with a code-level root cause
  - page errors / failed network calls observable after a successful login
  - a defect or security weakness in the app's own code

Use **OWNER** when the issue needs HUMAN action that the bot can't
take:
  - site unreachable (DNS, network, infra)
  - bot is explicitly denied access to the deployed site (Entra
    "access denied" page, not a 5xx) — only the human can grant
    the bot access
  - infrastructure, TLS, WAF or DNS configuration; credentials to
    rotate or grant; a product decision

## PHASE 1 — pipeline check

Use the github MCP to fetch workflow runs ON THIS COMMIT ONLY:
  - github__list_workflow_runs (filter by head_sha=__HEAD_SHA__)

**If the call returns ZERO runs for __HEAD_SHA__** — that's not a
failure. It means the repo's workflows are not configured to fire
on the push event that created this commit (commonly because the
workflows are gated on `pull_request`, or have `paths:`/`branches:`
filters that excluded the change). It is **NOT a CI failure**.

In that case:
  - Do **NOT** look at sibling commits, the most-recent PR, or the
    most-recent run on the branch.
  - Do **NOT** stage a draft.
  - Log one line: "[tester] no workflow runs for __HEAD_SHA__ — pipeline not configured to fire on this push; treating as healthy"
  - Proceed to PHASE 2.

If there are runs and ALL succeeded → log it, proceed to PHASE 2.

If at least one run for __HEAD_SHA__ has `conclusion != "success"`:
  - github__list_workflow_jobs to find the failed job(s).
  - github__download_workflow_run_logs (or
    github__get_workflow_run_usage) to extract the error message.
  - For each distinct failure, stage one draft:
      __DRAFTS_DIR__/01-pipeline-<workflow-name>.json
    with:
      - title: "CI failure: <workflow> on commit __HEAD_SHA__"
      - body: which job failed, error excerpt (≤ 50 lines), and a
        one-sentence root-cause hypothesis if obvious from the log
      - assigneeRole: "BOT"
      - media: [] (logs are text; usually no screenshot needed)
  - STOP the deployment testing after staging — the site is likely not
    in a testable state — but still run whichever of PHASES 5 and 7 are
    switched on: they read the SOURCE, which exists regardless of
    whether the deploy went out. Then finish at PHASE 8.

## PHASE 2 — find deployed website URL

Search the local checkout for a deployed URL. Sources, in order:
  1. `.github/workflows/*.yml` — `--hostname`, `--name`, custom
     domain refs in deploy steps, env vars like `WEBAPP_URL`.
  2. `terraform/*.tf`, `*.tfvars`, terraform outputs.
  3. `README.md` / `README*` — "Deployment" or "Access" sections.
  4. `k8s/`, `kustomize`, `helm` — ingress annotations, host names.
  5. Cluster: `kubectl get ingress --all-namespaces` (only if the
     deploy obviously uses k8s and `kubectl` is wired).

Filter:
  - **Prefer** URLs that mention "dev" / no env at all.
  - **Ignore** URLs explicitly tagged "prod" or "test".
  - If a URL has no env tag in its name, treat it as dev.

If no URL is discoverable → say so (`[tester] no deployed URL found`)
and skip to PHASE 5. The source-reading phases do not need a site.

## PHASE 3 — browser open + login

Use the browser plugin (use $BROWSER_PROFILE for isolation across
parallel testers) to navigate to the URL.

If the page does not load within 30 seconds (timeout, network error,
non-Entra 5xx page):
  - Take a screenshot. Note its `mediaPath`.
  - Stage __DRAFTS_DIR__/03-unreachable.json:
    - title: "Test site unreachable on __HEAD_SHA__"
    - body: URL, HTTP status if any, short description of the
      failure mode. Do NOT mention the screenshot — the wrapper
      attaches it.
    - assigneeRole: "OWNER"
    - media: [{"path": "<mediaPath>", "alt": "unreachable page"}]
  - Skip to PHASE 5.

If the page loads and shows a Microsoft Entra login:
  - Drive the autonomous login per TOOLS-entra.md — use
    `$ENTRA_USERNAME` / `$ENTRA_PASSWORD` / `entra-totp` for MFA.
  - DO NOT ask the user to log in. DO NOT print the URL/code in
    chat. The browser plugin + entra-totp helper let you complete
    the login end-to-end with zero user interaction.

If Entra explicitly shows "AADSTS…" access-denied / consent-
required / not-assigned-to-app errors (NOT a timeout or 5xx):
  - Screenshot. Note its `mediaPath`.
  - Stage __DRAFTS_DIR__/03-access-denied.json:
    - title: "Bot is denied access to test site on __HEAD_SHA__"
    - body: the exact Entra error code + message, what permission
      / role / app-assignment is needed
    - assigneeRole: "OWNER"
    - media: [{"path": "<mediaPath>", "alt": "entra access-denied page"}]
  - Skip to PHASE 5.

## PHASE 4 — exercise the site

You're now logged in. Test the page generically:
  - Navigate around (top-level routes, menu items)
  - Try forms (fill with plausible test data, submit)
  - Click buttons / links

### JavaScript console + page errors — check on EVERY page

A page can return HTTP 200 and still be broken: a blank/white screen, a
half-rendered view, or a control that silently does nothing is almost always
a JavaScript error. So **actively read the console — don't just glance at the
page**:

  - After EACH navigation / route change, use the browser plugin to pull the
    console output AND uncaught page errors (console messages + `pageerror` /
    unhandled exceptions). Query it explicitly every time — errors are NOT
    surfaced to you automatically, and a clean-looking screenshot does not mean
    the console is clean.
  - Treat as a finding ANY of: a console `error`-level message; an uncaught
    exception; a failed module/chunk/script/stylesheet load; `Uncaught
    SyntaxError` / `Unexpected token '<'` (HTML returned where JS/JSON was
    expected — a classic broken-asset-path symptom); a CSP or CORS failure;
    a failed `fetch`/XHR.
  - A blank or visually-empty page is itself a finding: open the console,
    capture the first error, and screenshot it — do not pass it as "loads OK".
  - Also watch the network panel for 4xx / 5xx responses.

For each DISTINCT error class (don't open 20 drafts for the same console
message repeating on every page):
  - Take a screenshot. Note its `mediaPath`.
  - For HTTP errors: try to pull the corresponding cloud-side log
    via `az monitor app-insights query` / `kubectl logs` / similar
    (use `$AZURE_CONFIG_DIR` so parallel testers don't fight over
    the same az profile)
  - Stage __DRAFTS_DIR__/04-error-<n>.json:
    - title: short, error-class summary (include the key console text, e.g.
      the exception name/message, so repeats of the same error collapse)
    - body: URL path that triggered it, the **exact** console error text,
      network excerpt, cloud-log excerpt if available. Do NOT mention the
      screenshot in the body — the wrapper attaches it.
    - assigneeRole: "BOT"
    - media: [{"path": "<mediaPath>", "alt": "<short error description>"}]

Test in common, not deeply. The point is broad surface coverage.

PROMPT_HEAD_EOF

read -r -d '' PROMPT_TAIL <<'PROMPT_TAIL_EOF' || true

## PHASE 9 — finalize

Print a brief summary on stdout:
  - HEAD tested: __HEAD_SHA__
  - Number of drafts staged in __DRAFTS_DIR__
  - One-line description of each
  - The outcome of EACH optional phase, stated SEPARATELY and named:
    **SAST**, **pen test**, **AI code review** — each as exactly one of
    `clean`, `N findings`, `skipped` (switched off, or not authorised —
    give the reason), or `inconclusive` (it did not finish: still
    running, timed out, errored, or no source checkout). Never fold
    `inconclusive` into `skipped` or `clean`: a scan that did not
    complete has told you nothing, and reporting it as clean hides real
    exposure.

**Then your VERY LAST output line, on its own line with no surrounding
text, must be exactly:**

    TESTER_DONE __HEAD_SHA__

The wrapper greps for that prefix on a tail of the log, then
terminates this agent process within ~10 seconds and processes
your drafts. Without that line the wrapper has no way to know you
are finished and will hold the agent open until the per-turn
timeout, wasting most of an hour.

## Reminders

  - You have NO write access to the repo. No commits.
  - You don't see existing issues — don't look. The wrapper does the
    duplicate check against open issues before it files anything.
  - DRAFTS_DIR for this run: __DRAFTS_DIR__
  - If you find yourself wanting to fix a bug — STOP. Stage the
    issue and let the issue-solver handle it.

Begin.
PROMPT_TAIL_EOF

# OPTIONAL per-project instructions from the target repo. Absent => an empty
# string => the prompt is unchanged, so a repository that ships no such file
# behaves exactly as it did before. Read at the COMMIT UNDER TEST, not at
# whatever is newest: the rest of this run is about that commit, and the
# instructions it runs under must not change mid-flight.
PROJECT_ANNOTATIONS_BLOCK=""
if command -v detect_project_annotations_from_dir >/dev/null 2>&1; then
  detect_project_annotations_from_dir "$PROJECT_DIR"
  PROJECT_ANNOTATIONS_BLOCK="$(project_annotations_block 2>/dev/null || true)"
  echo "[annotations] $REPO: ${PROJECT_ANNOTATIONS:-none}"
fi

PROJECT_INSTRUCTIONS=""
if command -v load_project_instructions >/dev/null 2>&1; then
  PROJECT_INSTRUCTIONS="$(load_project_instructions \
    "CLAWCODE-tester-instructions.md" "$HEAD_SHA" "$PROJECT_DIR" \
    2>/dev/null || true)"
fi
if [ -n "$PROJECT_INSTRUCTIONS" ]; then
  echo "[instructions] CLAWCODE-tester-instructions.md found — honouring it this run"
else
  echo "[instructions] no CLAWCODE-tester-instructions.md in $REPO — standard tester protocol"
fi

# Assemble: the fixed halves plus the four composed phases. Substitution is
# done with sed -i on the assembled file rather than by bash interpolation, so
# the prompt body can contain any characters (no escape hazard).
PROMPT_FILE="$(mktemp -t tester-prompt.XXXXXX)"
{
  printf '%s\n\n' "$PROMPT_HEAD"
  printf '%s\n\n' "$SAST_STEP"
  printf '%s\n\n' "$SECURITY_STEP"
  printf '%s\n\n' "$CODEREVIEW_STEP"
  printf '%s\n' "$CONSOLIDATE_STEP"
  printf '%s\n' "$PROMPT_TAIL"
  if [ -n "$PROJECT_ANNOTATIONS_BLOCK" ]; then
    printf '\n%s\n' "$PROJECT_ANNOTATIONS_BLOCK"
  fi
  if [ -n "$PROJECT_INSTRUCTIONS" ]; then
    printf '\n%s\n' "$PROJECT_INSTRUCTIONS"
  fi
} > "$PROMPT_FILE"
sed -i \
  -e "s|__REPO__|$REPO|g" \
  -e "s|__HEAD_SHA__|$HEAD_SHA|g" \
  -e "s|__DEFAULT_BRANCH__|$DEFAULT_BRANCH|g" \
  -e "s|__DRAFTS_DIR__|$DRAFTS_DIR|g" \
  "$PROMPT_FILE"

# Prepend the bot's persona (IDENTITY.md) and voice (SOUL.md) so they
# are in this turn's context without an extra tool call. Done via file
# concatenation (not sed) because IDENTITY/SOUL bodies can contain |,
# &, / and other sed-special characters.
PROMPT_FILE_FULL="$(mktemp -t tester-prompt-full.XXXXXX)"
{
  echo "## Your identity & voice"
  echo
  echo "The runtime mounts your persona at \`workspace/IDENTITY.md\` and your"
  echo "voice at \`workspace/SOUL.md\`. They are inlined below so you have"
  echo "them in this turn's context."
  echo
  echo "When you write text a human will read — issue draft bodies, ASK"
  echo "questions in commit-comments, the final stdout summary — use this"
  echo "voice. The role rules below (no commits, no PRs, draft schema,"
  echo "PHASE 1-9) still bind; they describe **what** to do."
  echo "IDENTITY.md / SOUL.md describe **how to sound**."
  echo
  echo "### workspace/IDENTITY.md"
  cat "$HOME/.openclaw/workspace/IDENTITY.md" 2>/dev/null || echo "(IDENTITY.md unreadable)"
  echo
  echo "### workspace/SOUL.md"
  cat "$HOME/.openclaw/workspace/SOUL.md" 2>/dev/null || echo "(SOUL.md unreadable)"
  echo
  echo "---"
  echo
  cat "$PROMPT_FILE"
} > "$PROMPT_FILE_FULL"
mv "$PROMPT_FILE_FULL" "$PROMPT_FILE"

# Concurrency gate — take a slot immediately before the agent invocation,
# never earlier: holding one through the clone or the API work above would
# starve the other subsystems for work that never touches the model.
#
# LOW priority, and that is the whole point of setting it: the gate defaults
# to `high`, and a tester at high priority reintroduces exactly the starvation
# the gate exists to prevent — its run is the longest of the three, so winning
# a first-come race lets it sit on a slot for an hour while issues and pull
# requests queue behind it.
#
# Yielding here is free: LAST_HEAD_FILE is only written after the agent runs,
# so this HEAD stays untested and the next tick picks it up again.
SLOT_NAME="tester ${REPO##*/}"
SLOT_PRIORITY=low
if command -v acquire_agent_slot >/dev/null 2>&1; then
  if ! acquire_agent_slot; then
    echo "[slot] no agent slot free — yielding this tick (HEAD $HEAD_SHA stays untested, next tick retries)"
    exit 0
  fi
fi

# Remember where the log was before the agent runs — used by the
# post-agent fallback to extract just THIS run's MEDIA: lines for
# drafts that didn't fill in media[].
RUN_START_LINE="$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)"
echo "[tester] invoking agent (session-id=$SESSION_ID)"

# Run the agent in the background under `setsid` so it becomes its
# own session/process-group leader. That way the watchers can signal
# the entire descendant tree with `kill -- -$AGENT_PID` — important
# because openclaw rewrites its own argv to "openclaw" (no flags,
# no session-id), so `pkill -f` against the original invocation
# matches nothing.
setsid openclaw agent --local \
  ${AGENT_MODEL_ARGS[@]+"${AGENT_MODEL_ARGS[@]}"} \
  --timeout "$AGENT_TURN_TIMEOUT" \
  --session-id "$SESSION_ID" \
  --message "$(cat "$PROMPT_FILE")" \
  </dev/null >>"$LOG_FILE" 2>&1 &
AGENT_PID=$!

# Common kill helper used by both watchers. Tries the process group
# first (so child openclaw-agent + chromium get the signal too),
# then falls back to a direct PID signal.
agent_kill() {
  local sig="${1:-TERM}"
  kill "-$sig" -- "-$AGENT_PID" 2>/dev/null \
    || kill "-$sig" "$AGENT_PID" 2>/dev/null \
    || true
}

# Watcher 1 — sentinel detection (the fast-exit path).
# The prompt instructs the agent to emit `TESTER_DONE <sha>` as its
# very last stdout line. openclaw's --local mode doesn't always
# produce a clean stop_reason=end_turn, so without this the agent
# can sit idle for up to AGENT_TURN_TIMEOUT after staging drafts.
# When we see the sentinel, give a grace window for any in-flight
# tool call to finish, then SIGTERM the agent's process group.
(
  tail -n 0 -F "$LOG_FILE" 2>/dev/null | while IFS= read -r line; do
    case "$line" in
      TESTER_DONE*)
        sleep 10
        agent_kill TERM
        # If TERM isn't honored within 15 s, escalate to KILL so the
        # wrapper never hangs waiting on a wedged agent.
        for _ in 1 2 3 4 5; do
          sleep 3
          kill -0 "$AGENT_PID" 2>/dev/null || break
        done
        kill -0 "$AGENT_PID" 2>/dev/null && agent_kill KILL
        break
        ;;
    esac
  done
) &
SENTINEL_WATCHER_PID=$!

# Watcher 2 — hard wall-clock backstop.
# If the sentinel never fires (model OOM, tool-error loop, refusal,
# missing sentinel line), enforce MAX_LIFETIME_SECONDS so the runner
# can't camp on the pod for an hour.
(
  sleep "$MAX_LIFETIME_SECONDS"
  echo "[tester] MAX_LIFETIME ($MAX_LIFETIME_SECONDS s) reached — terminating agent" >> "$LOG_FILE"
  agent_kill TERM
  sleep 15
  kill -0 "$AGENT_PID" 2>/dev/null && agent_kill KILL
) &
LIFETIME_WATCHER_PID=$!

# Wait for the agent (in its own process group) to exit. `wait` on
# a backgrounded child returns the child's exit status.
wait "$AGENT_PID"
AGENT_EXIT=$?
if [ "$AGENT_EXIT" -ne 0 ]; then
  echo "[tester] agent exited non-zero ($AGENT_EXIT) — proceeding to drafts processing"
fi

# Watchers will be killed by the EXIT trap; do it eagerly here too
# so they don't keep tailing the log after we move on.
kill "$SENTINEL_WATCHER_PID" 2>/dev/null
kill "$LIFETIME_WATCHER_PID" 2>/dev/null
SENTINEL_WATCHER_PID=""
LIFETIME_WATCHER_PID=""

# The model is done with; hand the slot back before the issue filing, which
# is pure API work and can take a while with screenshots to upload.
command -v release_agent_slot >/dev/null 2>&1 && release_agent_slot

rm -f "$PROMPT_FILE"

# ---- post-agent: create issues from drafts ------------------------

echo "[tester] processing drafts in $DRAFTS_DIR"

# Extract MEDIA: paths the agent emitted after RUN_START_LINE. The
# browser plugin prints `MEDIA:/home/node/.openclaw/media/browser/
# <uuid>.png` for each screenshot it captures. We pass these into
# the upload helper as a fallback for drafts that didn't populate
# `media[]` themselves (MiniMax sometimes skips the new field).
# Joined with ":" because paths never contain that character.
RUN_MEDIA="$(awk -v s="$RUN_START_LINE" \
  'NR>s && /^MEDIA:/ {sub(/^MEDIA:/, ""); printf "%s%s", sep, $0; sep=":"}' \
  "$LOG_FILE" 2>/dev/null)"
if [ -n "$RUN_MEDIA" ]; then
  echo "[tester] run captured $(echo "$RUN_MEDIA" | tr ':' '\n' | wc -l) screenshot(s)"
fi
FALLBACK_USED=""

# ---- dedup guard ---------------------------------------------------
# The tester is stateless per run (it does NOT read existing issues while
# testing — see "YOUR ROLE"), so without this a bug that persists across
# commits gets re-filed on every run. Collect the titles of currently-OPEN
# issues; skip any draft that reports the same thing.
#
# It reads ALL open issues, not only `tester`-labelled ones: the optional scan
# passes surface long-standing weaknesses that a human may well have filed
# first, and a duplicate of a human's issue is just as much noise as a
# duplicate of our own. Only OPEN issues suppress a draft — the issue-solver
# closes what it fixes, so a closed one CAN be re-filed if it regresses, which
# is a regression report and worth having.
EXISTING_TITLES_FILE="$DRAFTS_DIR/.open-issue-titles"
"${FORGE[@]}" open-issues 2>/dev/null \
  | python3 -c "
import sys, json
try: data = json.load(sys.stdin)
except Exception: data = []
# Change requests are already filtered out on the other side of the seam: on
# at least one host they come back from the issues collection looking like
# issues, and counting them here made the tester suppress a draft because it
# matched the title of its own earlier report.
for i in data if isinstance(data, list) else []:
    t = (i.get('title') or '').strip()
    if t: print(' '.join(t.split()))
" > "$EXISTING_TITLES_FILE" 2>/dev/null

# Compare by MEANING, not by identical wording. Two normalisations do the
# work, and both come from findings that were re-filed every single tick:
#   - commit SHAs are stripped, so "CI failure: build on commit abc1234"
#     matches the same failure on the next commit;
#   - a `🔒 Security:` prefix is stripped, so the same weakness reported by a
#     human and by a scan pass is one issue.
# Beyond that it is a token-overlap test, which catches the routine rewordings
# ("missing HSTS header" vs "no HSTS header on responses") without collapsing
# genuinely different findings.
draft_is_duplicate() {  # $1 = draft path -> 0 when an open issue already says it
  DRAFT="$1" EXISTING="$EXISTING_TITLES_FILE" python3 -c "
import json, os, re, sys

STOP = {'the', 'a', 'an', 'on', 'in', 'of', 'for', 'to', 'and', 'is', 'at',
        'with', 'commit', 'when', 'after'}

def norm(t):
    t = (t or '').lower().replace(chr(0x1F512), ' ')
    t = re.sub(r'\b[0-9a-f]{7,40}\b', ' ', t)   # commit shas
    t = re.sub(r'[^a-z0-9]+', ' ', t)
    return ' '.join(t.split())

def tokens(t):
    return {w for w in norm(t).split() if w not in STOP}

try:
    with open(os.environ['DRAFT'], encoding='utf-8') as f:
        title = json.load(f).get('title') or ''
except Exception:
    sys.exit(1)
mine, mine_t = norm(title), tokens(title)
if not mine:
    sys.exit(1)
try:
    with open(os.environ['EXISTING'], encoding='utf-8') as f:
        existing = [l.strip() for l in f if l.strip()]
except Exception:
    existing = []
for other in existing:
    theirs, theirs_t = norm(other), tokens(other)
    if mine == theirs:
        print(other); sys.exit(0)
    if not mine_t or not theirs_t:
        continue
    overlap = len(mine_t & theirs_t) / len(mine_t | theirs_t)
    if overlap >= 0.7:
        print(other); sys.exit(0)
sys.exit(1)
" 2>/dev/null
}

CREATED_ISSUES=()
DRAFT_COUNT=0
SKIPPED_DUPLICATES=0
for draft in "$DRAFTS_DIR"/*.json; do
  [ -f "$draft" ] || continue
  DRAFT_COUNT=$((DRAFT_COUNT + 1))

  # Skip drafts that duplicate an already-open issue.
  if DUP="$(draft_is_duplicate "$draft")"; then
    SKIPPED_DUPLICATES=$((SKIPPED_DUPLICATES + 1))
    echo "[tester] skipping duplicate — open issue already reports this: $DUP"
    continue
  fi

  # Upload any screenshots referenced in draft.media[] to the orphan
  # `tester-screenshots` branch and rewrite the draft body to link
  # to them. The helper also strips stale "tester-screenshot:<name>"
  # placeholder lines from the body. Only the first draft in a run
  # gets the RUN_MEDIA fallback, so multi-issue runs don't end up
  # double-attaching the same screenshots to every issue.
  if [ -z "$FALLBACK_USED" ]; then
    THIS_FALLBACK="$RUN_MEDIA"
    FALLBACK_USED=1
  else
    THIS_FALLBACK=""
  fi
  GITHUB_TOKEN="$GITHUB_TOKEN" REPO="$REPO" HEAD_SHA="$HEAD_SHA" \
    TESTER_FALLBACK_MEDIA="$THIS_FALLBACK" \
    /usr/local/bin/tester-upload-screenshots "$draft" \
    || echo "[tester] note: screenshot upload had errors for $draft (continuing)"

  # Resolve assigneeRole → actual login. Map at create-time so the
  # agent never sees real GitHub logins (keeps the prompt
  # identity-agnostic per spec).
  #
  # BOT means the issue-solver picks it up on its next tick; OWNER means the
  # repository owner, for the findings no amount of app code can fix —
  # infrastructure, TLS, credentials, a product decision.
  payload="$(BOT_LOGIN_VAL="$BOT_LOGIN" OWNER_LOGIN="$REPO_OWNER" \
    python3 -c "
import sys, json, os
with open('$draft') as f:
    d = json.load(f)
role = d.pop('assigneeRole', 'OWNER')
login = os.environ['BOT_LOGIN_VAL'] if role == 'BOT' else os.environ['OWNER_LOGIN']
d['assignees'] = [login]
# Tag the issue so the user can tell tester-created issues apart from
# human-created ones at a glance in the GitHub list view.
labels = d.get('labels', [])
if 'tester' not in labels:
    labels.append('tester')
# A finding the agent titled with the security prefix carries the label too,
# so a human can filter for them without reading every title.
if (d.get('title') or '').lstrip().startswith('\U0001F512') and 'security' not in labels:
    labels.append('security')
d['labels'] = labels
print(json.dumps(d))
" 2>/dev/null)"
  if [ -z "$payload" ]; then
    echo "[tester] WARN: could not parse draft $draft — skipping"
    continue
  fi
  # Title, body, labels and assignee travel as arguments and a FILE rather
  # than as a hand-built payload: the body is agent output full of backticks
  # and quotes, and the shape of the request is the forge's business.
  _title="$(printf '%s' "$payload" | python3 -c \
    "import sys,json; print(json.load(sys.stdin).get('title',''))" 2>/dev/null)"
  _labels="$(printf '%s' "$payload" | python3 -c \
    "import sys,json; print(','.join(json.load(sys.stdin).get('labels') or []))" 2>/dev/null)"
  _assignees="$(printf '%s' "$payload" | python3 -c \
    "import sys,json; print(','.join(json.load(sys.stdin).get('assignees') or []))" 2>/dev/null)"
  _bodyf="$(mktemp)"
  printf '%s' "$payload" | python3 -c \
    "import sys,json; sys.stdout.write(json.load(sys.stdin).get('body') or '')" \
    > "$_bodyf" 2>/dev/null
  n="$("${FORGE[@]}" create-issue --title "$_title" --body-file "$_bodyf" \
        --labels "$_labels" --assignees "$_assignees" 2>/dev/null)"
  rm -f "$_bodyf"
  if [ -n "$n" ]; then
    CREATED_ISSUES+=("#$n")
    echo "[tester] created issue $REPO #$n"
  else
    echo "[tester] WARN: could not file the issue for $draft"
  fi
done

# ---- mark HEAD as tested + write summary --------------------------

echo "$HEAD_SHA" > "$LAST_HEAD_FILE"

# Which passes ran is part of the result, not a detail: "no issues created"
# means something very different when every optional pass was switched off.
if [ "$PENTEST_ACTIVE" = "1" ]; then
  PENTEST_LINE="pen test **on** (authorised hosts: $PENTEST_ALLOWED_HOSTS)"
else
  PENTEST_LINE="pen test **skipped** — $PENTEST_SKIP_REASON"
fi

SUMMARY_FILE="$SUMMARIES_DIR/${REPO//\//__}-$HEAD_SHA.md"
{
  echo "# tester: $REPO @ $HEAD_SHA"
  echo "_branch: $DEFAULT_BRANCH, $(date -Iseconds)_"
  echo
  echo "Passes: SAST **$(_onoff "$SAST_ON")** · AI code review **$(_onoff "$CR_ON")** · $PENTEST_LINE"
  echo "Deploy checks: $DEPLOY_CHECKS"
  echo
  # THE HEADLINE MUST NOT OUTRANK THE DEPLOY STATE.
  #
  # "no issues created" and "all tests passed" are not the same claim, and
  # conflating them turned a broken environment into a green report. The pen
  # test is SKIPPED when the deploy checks did not succeed — deliberately,
  # since there is no verified deployment to scan — and the run then said
  # "✅ all tests passed" and sent that to Telegram. Observed on
  # ultimate-web-stack-dev: mongodb crash-looping and every web pod stuck on a
  # missing secret for fourteen hours, while every tester run reported success.
  #
  # A failed deploy is the loudest fact the tester has. It says nothing about
  # the code and everything about whether the code was ever RUN, so it leads.
  if [ "$DEPLOY_CHECKS" = "failed" ]; then
    echo "❌ **the deployment of this commit FAILED** — nothing was verified against a running system."
    echo
    echo "Whatever passed below was checked by reading the code, not by running it. The deployment itself is the finding: until it is fixed, a green line here means only that no NEW problem was found in the source."
    if [ "${#CREATED_ISSUES[@]}" != "0" ]; then
      echo
      echo "🔍 ${#CREATED_ISSUES[@]} issue(s) created:"
      printf '  - %s\n' "${CREATED_ISSUES[@]}"
    fi
  elif [ "$DEPLOY_CHECKS" != "green" ]; then
    # pending / none / unknown. Not a failure, but not a pass either: the
    # checks that need a running system did not run.
    echo "⚠️ no verified deployment of this commit (deploy checks: $DEPLOY_CHECKS) — the checks that need a running system did not run."
    if [ "${#CREATED_ISSUES[@]}" = "0" ]; then
      echo
      echo "No issues created from the passes that did run."
    else
      echo
      echo "🔍 ${#CREATED_ISSUES[@]} issue(s) created:"
      printf '  - %s\n' "${CREATED_ISSUES[@]}"
    fi
  elif [ "${#CREATED_ISSUES[@]}" = "0" ]; then
    echo "✅ all tests passed, no issues created"
  else
    echo "🔍 ${#CREATED_ISSUES[@]} issue(s) created:"
    printf '  - %s\n' "${CREATED_ISSUES[@]}"
  fi
  if [ "$SKIPPED_DUPLICATES" -gt 0 ]; then
    echo
    echo "_$SKIPPED_DUPLICATES finding(s) already tracked in an open issue — not re-filed._"
  fi
} > "$SUMMARY_FILE"
echo "[tester] summary written to $SUMMARY_FILE"
cat "$SUMMARY_FILE"

# Post the summary as a commit comment so the repo subscriber gets a
# GitHub notification.
SUMMARY_BODY="$(cat "$SUMMARY_FILE")"
"${FORGE[@]}" comment-on-commit --sha "$HEAD_SHA" --body-file "$SUMMARY_FILE" \
  >/dev/null 2>&1 \
  || echo "[tester] note: could not post the commit comment (continuing)"

# Telegram delivery. `telegram-notify` resolves the owner's paired chat from
# openclaw's own state and FAILS OPEN on every path, so it needs no `|| true`
# — a notification must never be the thing that fails a run.
telegram-notify "$SUMMARY_BODY"

echo "[$(date -Iseconds)] tester exit  repo=$REPO  sha=$HEAD_SHA  drafts=$DRAFT_COUNT  created=${#CREATED_ISSUES[@]}  duplicates-skipped=$SKIPPED_DUPLICATES"

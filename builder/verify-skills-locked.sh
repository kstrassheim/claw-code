#!/bin/sh
# verify-skills-locked: the bot must not be able to give itself new skills.
#
# Run from the repository root. Used by the `verify-skills-locked` CI job on
# every pull request (and on main), and runnable by hand:
#
#     sh builder/verify-skills-locked.sh
#
# WHAT THIS PROVES, AND WHAT IT DOES NOT
# --------------------------------------
# Every capability the bot has is supposed to be in this repository and to
# have gone through a pull request. A skill the agent acquires at RUNTIME is
# neither — and it is the shortest way around every other control, the
# allowed-projects permission list included: a skill is just instructions the
# agent will follow, so one it wrote for itself can tell it to do anything
# the sandbox physically permits.
#
# These are STATIC checks over the repository. They prove the things that can
# be proven from the source:
#
#   1. the image keeps only the bundled skills BUNDLED_SKILLS_ALLOWED names
#      (it names none), and the Dockerfile still asserts the strip took
#      effect — the removal alone is not enough, because `rm -rf` on a path
#      upstream has moved exits 0 and removes nothing, which is silent and
#      total failure
#   2. no self-extension skill is re-enabled through the plugin allowlist
#   3. the skills loadable in the pod are EXACTLY the bot's own — every one
#      this repo defines is mounted, and nothing else is
#   4. the init container makes workspace/skills root-owned and read-only,
#      after the recursive chown that would otherwise undo it, so no FURTHER
#      skill can appear at runtime
#
# The image is checked FOR REAL as well, at build time, by the assertion in
# builder/Dockerfile that check 1 is guarding — that is what inspects the
# actual filesystem of the actual image, and it fails the pull request's
# image-build job if a denied skill is present.
#
# Checks 3 and 4 are the two halves of "the bot's own skills, and no others":
# 3 fixes the set at deploy time from reviewed ConfigMaps, 4 stops anything
# being added to it afterwards. Neither is sufficient alone — openclaw loads
# whatever directories it finds under <workspaceDir>/skills/, and that path
# is on the writable workspace PVC.
set -eu

FAIL=0
ALLOW_FILE="builder/BUNDLED_SKILLS_ALLOWED"
DOCKERFILE="builder/Dockerfile"
CONFIG="k8s/010-openclaw-config.yaml"
DEPLOYMENT="k8s/020-deployment.yaml"
# Sentinels the guarded code emits, so removing a guard is itself a CI failure
# rather than a quiet loss of coverage. Both are checked against the files with
# comments stripped — a guard you can satisfy by writing prose about it is not
# a guard.
ASSERT_SENTINEL="SKILLS-LOCKED-ASSERT"
READONLY_SENTINEL="SKILLS-DIR-READONLY"
LOCKDOWN="builder/lock-skill-dirs.sh"

fail() { echo "  FAIL: $*" >&2; FAIL=$((FAIL + 1)); }
ok()   { echo "  ok:   $*"; }

for f in "$ALLOW_FILE" "$DOCKERFILE" "$CONFIG" "$DEPLOYMENT"; do
  [ -f "$f" ] || { echo "ERROR: $f not found — run from the repo root" >&2; exit 2; }
done

# Bundled skills we keep. Empty is the expected and correct state — the bot's
# own skills are ConfigMap mounts under the workspace, not baked into the
# image, so nothing upstream ships needs to survive.
KEPT="$(sed 's/#.*//' "$ALLOW_FILE" | tr -d '\r' | awk 'NF')"

# The self-extension trio, named here because check 2 is about the PLUGIN
# allowlist — a different mechanism from the bundled-skill strip, which an
# empty allowlist therefore says nothing about. See $ALLOW_FILE for what each
# one does.
SELF_EXTENSION="clawhub skill-creator mcporter"

# Comments stripped: a prose mention of a skill name or of the sentinel must
# never be able to satisfy a check. (It did, in the first draft of this
# script — the explanatory comment in the Dockerfile contained a skill path,
# so the grep passed while nothing was actually being removed.)
DOCKER_CODE="$(sed 's/^[[:space:]]*#.*//' "$DOCKERFILE" | awk 'NF')"

echo "== 1. the image keeps only the bundled skills the allowlist names =="
if [ -z "$KEPT" ]; then
  ok "$ALLOW_FILE keeps no bundled skill — the bot has only its own four"
else
  ok "$ALLOW_FILE keeps: $(echo "$KEPT" | tr '\n' ' ')"
  for k in $KEPT; do
    for bad in $SELF_EXTENSION; do
      [ "$k" = "$bad" ] && fail "$ALLOW_FILE keeps '$k', which exists to let
        the agent install or author skills at runtime — the one thing this
        whole check is for"
    done
  done
fi

if printf '%s\n' "$DOCKER_CODE" | grep -q 'COPY .*BUNDLED_SKILLS_ALLOWED'; then
  ok "$DOCKERFILE reads the list from $ALLOW_FILE"
else
  fail "$DOCKERFILE does not COPY BUNDLED_SKILLS_ALLOWED. The list must be
        what the build actually consumes, or editing it changes nothing."
fi

# A removal has to exist, and it has to be driven by a SEARCH rather than by a
# hardcoded directory. Skills ship in three places — /app/skills, inside
# extensions, and again in the built mirror — and a loop over one of them
# reports every skill it finds as stripped while the other two carry on
# shipping. That is what happened.
if printf '%s\n' "$DOCKER_CODE" | grep -q 'rm -rf'; then
  ok "$DOCKERFILE removes what the allowlist does not name"
else
  fail "$DOCKERFILE has no removal of unlisted skills at all"
fi

if printf '%s\n' "$DOCKER_CODE" | grep -q 'find /app -name SKILL.md'; then
  ok "the removal is driven by a search for loadable skills, not one directory"
else
  fail "$DOCKERFILE does not locate skills with 'find /app -name SKILL.md'.
        Skills ship in /app/skills, inside extensions, AND in the built mirror
        under /app/dist. A loop over a single hardcoded directory removes one
        of the three and reports success."
fi

# The CI re-check of the finished image must ask the IMAGE what is loadable,
# not search the filesystem itself. When it had its own search it drifted from
# the build's: it did not exclude node_modules, and two npm dependencies that
# ship a SKILL.md failed a build in which nothing was wrong. One definition,
# one implementation.
# Comments stripped before matching, for the same reason as the Dockerfile
# above: a guard you can satisfy by writing prose about it is not a guard.
# This exact check passed on a workflow that had been changed back to its own
# search, because the comment explaining why it should not still named the
# script.
WORKFLOW_CODE="$(cat .github/workflows/*.yml 2>/dev/null | sed 's/#.*//' | awk 'NF')"
if printf '%s\n' "$WORKFLOW_CODE" | grep -q 'list-loadable-skills'; then
  ok "the image re-check uses the image's own skill lister"
elif printf '%s\n' "$WORKFLOW_CODE" | grep -q 'name SKILL.md'; then
  fail "a workflow searches for SKILL.md itself instead of running
        list-loadable-skills in the image. Two implementations of one
        definition drift, and the drift fails builds that are fine."
else
  fail "no workflow re-checks the finished image for unlisted skills"
fi

if printf '%s\n' "$DOCKER_CODE" | grep -q "$ASSERT_SENTINEL"; then
  ok "$DOCKERFILE still asserts the removal took effect ($ASSERT_SENTINEL)"
else
  fail "$DOCKERFILE has no $ASSERT_SENTINEL assertion in its build steps.
        Removing the skills is not enough on its own: if upstream moves
        /app/skills the rm matches nothing, exits 0, and every bundled skill
        ships. The assertion is what turns that into a failed build."
fi

# The assertion has to be able to FAIL the build, not just print.
if printf '%s\n' "$DOCKER_CODE" | grep -q 'exit 1'; then
  ok "the assertion can fail the build"
else
  fail "no 'exit 1' in $DOCKERFILE's build steps — an assertion that only
        prints is not an assertion"
fi

echo "== 2. no self-extension skill is re-enabled through the plugin allowlist =="
ALLOW_LINE="$(grep -n '"allow"' "$CONFIG" || true)"
if [ -z "$ALLOW_LINE" ]; then
  fail "no plugins \"allow\" list found in $CONFIG — openclaw grants runtime
        capabilities only to allowlisted plugins, so losing the list is a
        loss of control, not a relaxation of one"
else
  ok "plugin allowlist present: $(echo "$ALLOW_LINE" | cut -c1-100)"
  for skill in $SELF_EXTENSION; do
    if echo "$ALLOW_LINE" | grep -q "\"$skill\""; then
      fail "$skill appears in the plugin allowlist in $CONFIG"
    else
      ok "$skill is not in the plugin allowlist"
    fi
  done
fi

echo "== 3. the loadable skills are exactly the ones this repo defines =="
# Every skill openclaw loads in the pod arrives as a subPath mount at
# workspace/skills/<name>/SKILL.md, fed by a ConfigMap volume. The bot's own
# skills are built this way and MUST be there; nothing else may be.
MOUNTED="$(grep -oE 'workspace/skills/[a-z0-9-]+/SKILL\.md' "$DEPLOYMENT" \
           | sed 's|workspace/skills/||; s|/SKILL\.md||' | sort -u)"
VOLUMES="$(grep -oE 'name: claw-code-skill-[a-z0-9-]+' "$DEPLOYMENT" \
           | sed 's|name: claw-code-skill-||' | sort -u)"
DEFINED="$(grep -rhoE '^  name: claw-code-skill-[a-z0-9-]+' k8s/*.yaml \
           | sed 's|^  name: claw-code-skill-||' | sort -u)"

if [ -z "$MOUNTED" ]; then
  fail "no skill mounts found in $DEPLOYMENT — the grep above has gone stale"
else
  ok "skills mounted: $(echo "$MOUNTED" | tr '\n' ' ')"
fi

# Both directions. A mount with no ConfigMap in this repo would be satisfied
# by whatever else in the cluster claims that name — i.e. by something outside
# review. A ConfigMap with no mount is a skill of the bot's own that was
# written and then silently never loaded.
for name in $MOUNTED; do
  if ! echo "$VOLUMES" | grep -qx "$name"; then
    fail "skill '$name' is mounted but has no claw-code-skill-$name volume"
  elif echo "$DEFINED" | grep -qx "$name"; then
    ok "$name ← ConfigMap claw-code-skill-$name, defined in k8s/"
  else
    fail "skill '$name' mounts ConfigMap claw-code-skill-$name, which no
        manifest in k8s/ defines — it would resolve to whatever else in the
        cluster claims that name"
  fi
done

for name in $DEFINED; do
  if echo "$MOUNTED" | grep -qx "$name"; then continue; fi
  fail "k8s/ defines the skill ConfigMap claw-code-skill-$name but
        $DEPLOYMENT never mounts it — one of the bot's own skills would not
        be loaded at all"
done

for skill in $SELF_EXTENSION; do
  if echo "$MOUNTED" | grep -qx "$skill"; then
    fail "self-extension skill '$skill' is mounted into the pod by $DEPLOYMENT"
  fi
done

echo "== 4. no FURTHER skill can be added at runtime =="
# The mounts above fix the set at deploy time, but openclaw loads whatever is
# in workspace/skills/ and that directory lives on the writable PVC. Without
# the lockdown the agent can simply mkdir itself a new one with its exec tool,
# and a skill is instructions it will follow — the way around every other
# control, this repo's allowed-projects list included.
DEPLOY_CODE="$(sed 's/^[[:space:]]*#.*//' "$DEPLOYMENT" | awk 'NF')"

# WHAT THIS SECTION PROVES, AND WHAT PROVES THE REST. Everything below reads
# text: that the init container calls the lockdown, in the right order, and
# that the lockdown names every load path known. It does NOT show that a
# root-owned 0555 directory actually stops uid 1000, nor that root can still
# mount into one — and the ConfigMap mounts depend on that second assumption.
# builder/verify-lockdown-effective.sh does that part, by running THE SAME
# lock-skill-dirs inside the built image and then attacking it as the agent.
# The two are meant to be read together; neither alone is the guarantee.
if printf '%s
' "$DEPLOY_CODE" | grep -q 'lock-skill-dirs'; then
  ok "the init container calls the lockdown (lock-skill-dirs)"
else
  fail "$DEPLOYMENT no longer calls lock-skill-dirs. openclaw loads any skill
        directory it finds, and those directories are on the writable PVC."
fi

[ -f "$LOCKDOWN" ] || { echo "ERROR: $LOCKDOWN not found" >&2; exit 2; }
LOCK_CODE="$(sed 's/^[[:space:]]*#.*//' "$LOCKDOWN" | awk 'NF')"

if printf '%s
' "$LOCK_CODE" | grep -q "$READONLY_SENTINEL"; then
  ok "$LOCKDOWN still emits $READONLY_SENTINEL"
else
  fail "$LOCKDOWN no longer emits $READONLY_SENTINEL"
fi

# Every known load path. workspace/skills is what loadWorkspaceSkillEntries
# scans; .claude/skills and .agents/skills are named in the openclaw bundle;
# HOME_DIR/skills is where `openclaw skills install --global` lands. Stripping
# the bundled skills took the agent's instructions away, not the CLI — the
# directories are what actually stop an install.
for d in 'workspace/skills' 'HOME_DIR/skills'          'workspace/.claude/skills' 'workspace/.agents/skills'; do
  if printf '%s
' "$LOCK_CODE" | grep -qF "$d"; then
    ok "$d is covered by the lockdown"
  else
    fail "$LOCKDOWN never mentions $d — a skill install targeting it would
        succeed"
  fi
done

if printf '%s
' "$LOCK_CODE" | grep -q 'chmod 0555'; then
  ok "the skill directories are made read-only"
else
  fail "no 'chmod 0555' in $LOCKDOWN"
fi

if printf '%s
' "$LOCK_CODE" | grep -q 'chown -R 0:0'; then
  ok "the skill directories are handed to root"
else
  fail "no 'chown -R 0:0' in $LOCKDOWN — a directory the agent's own uid owns
        can simply be chmod'ed back by the agent"
fi

# Ordering is the whole game: the generic chown -R 1000:1000 of the state dir
# would give the skills directories straight back to the agent if it ran after.
CHOWN_ALL_LINE="$(printf '%s
' "$DEPLOY_CODE" | grep -n 'chown -R 1000:1000' | head -1 | cut -d: -f1)"
LOCK_LINE="$(printf '%s
' "$DEPLOY_CODE" | grep -n 'lock-skill-dirs' | head -1 | cut -d: -f1)"
if [ -n "$CHOWN_ALL_LINE" ] && [ -n "$LOCK_LINE" ]; then
  if [ "$LOCK_LINE" -gt "$CHOWN_ALL_LINE" ]; then
    ok "the lockdown runs after the recursive chown, so it survives it"
  else
    fail "lock-skill-dirs runs BEFORE 'chown -R 1000:1000' of the state dir,
        which then hands the directories back to the agent and undoes it"
  fi
fi

# The functional test has to actually be wired in, or the only thing behind
# this whole section is the grepping above.
if grep -rq 'verify-lockdown-effective' .github/workflows/ 2>/dev/null; then
  ok "the image build attacks the lockdown in the built image (functional test)"
else
  fail "verify-lockdown-effective.sh is not run by any workflow in
        .github/workflows/ — nothing then proves the lockdown HOLDS, only
        that it is written down"
fi

echo "== 5. openclaw's own chat commands stay enabled =="
# The point of all of the above is to remove SKILLS. openclaw's built-in chat
# commands — /models and the rest — are a different mechanism: compiled into
# the binary and switched on by commands.native, not files under any skills/
# directory. Nothing here removes them, and nothing here should.
#
# It is checked anyway because the two are easy to confuse: commands.native
# and commands.nativeSkills sit on adjacent lines of the same config block,
# and "turn off the skill commands" is a plausible-sounding edit that would
# take /models with it. Losing them would be a silent regression — the bot
# just stops answering a command nobody thought to test.
NATIVE_LINE="$(grep -E '"native"[[:space:]]*:' "$CONFIG" || true)"
if [ -z "$NATIVE_LINE" ]; then
  fail "no commands.native setting in $CONFIG — openclaw's built-in chat
        commands (/models and the rest) are governed by it"
else
  case "$NATIVE_LINE" in
    *false*|*'"off"'*|*'"none"'*)
      fail "commands.native is disabled in $CONFIG:$NATIVE_LINE
        That switches off openclaw's built-in chat commands, /models
        included. Only SKILLS are meant to be removed here." ;;
    *)
      ok "commands.native is on:$(echo "$NATIVE_LINE" | cut -c1-60)" ;;
  esac
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "PASS: the agent cannot be handed a skill this repository did not review,"
  echo "      and openclaw's own commands are untouched."
  exit 0
fi
echo "FAILED: $FAIL check(s). The bot could gain a capability that never went"
echo "through a pull request — see the comments in $ALLOW_FILE." >&2
exit 1

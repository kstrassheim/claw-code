#!/bin/sh
# lock-skill-dirs: make every directory openclaw can load a skill from
# root-owned and read-only, so the agent cannot give itself one at runtime.
#
# Run AS ROOT from the fix-permissions init container (k8s/020-deployment.yaml),
# and — this is the point of it being a script — run again by
# builder/verify-lockdown-effective.sh inside the built image, which then tries
# to defeat it as uid 1000. The lockdown and the thing that proves the lockdown
# works are the same code; if they were two copies, the test would eventually be
# testing something the deployment no longer does.
#
# MUST run after any recursive `chown -R 1000:1000` of the state directory,
# which would otherwise hand these directories straight back to the agent.
#
# Env:
#   OPENCLAW_HOME  state dir (default /home/node/.openclaw). The test points
#                  this at a scratch directory.
#   AGENT_UID      the agent's uid (default 1000).
set -u

HOME_DIR="${OPENCLAW_HOME:-/home/node/.openclaw}"
AGENT_UID="${AGENT_UID:-1000}"

# Every path the image is known to load skills from:
#   workspace/skills         what loadWorkspaceSkillEntries scans, and where the
#                            four ConfigMap SKILL.md files are mounted
#   workspace/.claude/skills
#   workspace/.agents/skills named in the openclaw bundle; workspace-relative,
#                            so they sit on the writable PVC. Whether this
#                            configuration scans them is unverified — locked
#                            defensively, because an empty root-owned directory
#                            costs nothing and a missed load path costs the
#                            whole control.
#   skills                   where `openclaw skills install --global` puts the
#                            "shared managed skills directory"
SKILL_DIRS="$HOME_DIR/workspace/skills
$HOME_DIR/workspace/.claude/skills
$HOME_DIR/workspace/.agents/skills
$HOME_DIR/skills"

for d in $SKILL_DIRS; do
  mkdir -p "$d"
done

# The dot-directory PARENTS stay writable by the agent: openclaw may keep other
# state under .claude/ and .agents/, and locking a whole dot-directory on a
# guess is wider than the evidence supports. Only the skills leaf is sealed.
for p in "$HOME_DIR/workspace/.claude" "$HOME_DIR/workspace/.agents"; do
  [ -d "$p" ] && chown "$AGENT_UID:$AGENT_UID" "$p"
done

for d in $SKILL_DIRS; do
  # Root-owned AND read-only. Ownership matters as much as the mode: a
  # directory the agent's own uid owns can simply be chmod'ed back by the
  # agent, so 0555 alone would be decoration.
  chown -R 0:0 "$d"
  chmod 0555 "$d"
  # Each mounted skill's own directory too, so nothing can be dropped in
  # beside an existing SKILL.md.
  for s in "$d"/*/; do
    [ -d "$s" ] && chmod 0555 "$s"
  done
done

echo "SKILLS-DIR-READONLY — loadable skills are now fixed at:" \
  "$(ls -1 "$HOME_DIR/workspace/skills" 2>/dev/null | tr '\n' ' ')"

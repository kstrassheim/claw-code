#!/bin/sh
# verify-lockdown-effective: does the skills lockdown actually hold?
#
# Piped into the BUILT IMAGE as root by the docker-build job:
#     docker run --rm -i --user 0 --entrypoint sh "$IMG" \
#       < builder/verify-lockdown-effective.sh
#
# WHY, WHEN verify-skills-locked.sh ALREADY CHECKS THE LOCKDOWN
# ------------------------------------------------------------
# It doesn't, really. That script greps the deployment manifest for `chown -R
# 0:0`, `chmod 0555`, the four paths and their ordering. That proves the lines
# are written, not that they work. Everything the control rests on was
# unverified:
#
#   - can uid 1000 really not create a directory in a root-owned 0555 dir?
#   - can root (kubelet) still bind-mount a SKILL.md INTO one? the whole design
#     assumes yes — if not, the pod comes up with no skills at all
#   - does the lockdown script even run clean?
#   - did it lock too much? the agent must still write to workspace/ (AGENTS.md
#     and friends) or chat dispatch dies with EACCES
#
# So this runs the real lock-skill-dirs against a scratch state directory laid
# out like production, then tries to defeat it as the agent's own uid. Same
# script the init container runs — not a copy that can drift.
#
# Exit 0 = the lockdown holds and nothing legitimate was broken.
set -u

SCRATCH=/tmp/lockdown-test
FAILED=0
ok()   { echo "  ok:   $*"; }
bad()  { echo "  FAIL: $*" >&2; FAILED=$((FAILED + 1)); }

# `su` rather than a --user flag: the test needs BOTH identities in one run —
# root to apply the lockdown and stand in for kubelet, uid 1000 to attack it.
as_agent() { su node -s /bin/sh -c "$1" >/dev/null 2>&1; }

echo "=== setting up a state dir shaped like production ==="
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH/workspace/skills/developer" "$SCRATCH/workspace/skills/tester"
# stands in for the ConfigMap subPath mounts
echo "name: developer" > "$SCRATCH/workspace/skills/developer/SKILL.md"
echo "name: tester"    > "$SCRATCH/workspace/skills/tester/SKILL.md"
# and for the init container's recursive chown, which the lockdown must survive
chown -R 1000:1000 "$SCRATCH"
chmod -R u+rwX,g+rwX "$SCRATCH"

echo "=== applying the real lockdown ==="
OPENCLAW_HOME="$SCRATCH" sh /usr/local/bin/lock-skill-dirs || {
  echo "FATAL: lock-skill-dirs failed to run" >&2
  exit 1
}

echo
echo "=== the agent must NOT be able to add a skill ==="
for rel in workspace/skills workspace/.claude/skills workspace/.agents/skills skills; do
  if as_agent "mkdir '$SCRATCH/$rel/evil'"; then
    bad "uid 1000 created $rel/evil — the lockdown does not hold"
  else
    ok "$rel: mkdir denied"
  fi
  if as_agent "mkdir -p '$SCRATCH/$rel/evil2' && echo x > '$SCRATCH/$rel/evil2/SKILL.md'"; then
    bad "uid 1000 planted a SKILL.md under $rel"
  else
    ok "$rel: planting a SKILL.md denied"
  fi
done

# A skill is <dir>/SKILL.md, so writing beside an existing one matters too.
if as_agent "echo x > '$SCRATCH/workspace/skills/developer/EXTRA.md'"; then
  bad "uid 1000 wrote into an existing skill directory"
else
  ok "existing skill directories are sealed too"
fi

echo
echo "=== and must still be able to do its job ==="
# Over-locking is the other failure. The agent writes AGENTS.md and other
# state into workspace/; if that broke, chat dispatch dies with EACCES and the
# lockdown would have traded one problem for a worse one.
if as_agent "echo x > '$SCRATCH/workspace/AGENTS.md'"; then
  ok "workspace/ is still writable by the agent"
else
  bad "workspace/ is no longer writable — the lockdown went too far"
fi
for p in workspace/.claude workspace/.agents; do
  if as_agent "echo x > '$SCRATCH/$p/state.json'"; then
    ok "$p is still writable (only its skills/ leaf is sealed)"
  else
    bad "$p is not writable — only the skills leaf was meant to be locked"
  fi
done

echo
echo "=== kubelet must still be able to mount a SKILL.md ==="
# The design assumes root writes through the 0555 mode. If that is wrong, the
# ConfigMap subPath mounts fail and the pod starts with NO skills — which
# would look like the skills feature being broken, not like a permissions bug.
if echo "name: developer" > "$SCRATCH/workspace/skills/developer/SKILL.md" 2>/dev/null; then
  ok "root can still write into a locked skill directory"
else
  bad "root cannot write into the locked directory — the ConfigMap mounts
        would fail and the pod would come up with no skills at all"
fi
if mkdir -p "$SCRATCH/workspace/skills/reviewer" 2>/dev/null; then
  ok "root can still create a skill directory (kubelet pre-creating a mount)"
else
  bad "root cannot create a skill directory"
fi

rm -rf "$SCRATCH"
echo
if [ "$FAILED" -eq 0 ]; then
  echo "PASS: the lockdown holds against uid 1000, and nothing legitimate broke."
  exit 0
fi
echo "FAILED: $FAILED check(s) — the skills lockdown does not do what the" >&2
echo "manifest says it does." >&2
exit 1

#!/bin/sh
# probe-skill-dirs: ask the built image where openclaw puts skills.
#
# Piped into the image by the docker-build job:
#     docker run --rm -i --entrypoint sh "$IMG" < builder/probe-skill-dirs.sh
#
# WHY THIS EXISTS
# ---------------
# Removing the bundled skills (builder/BUNDLED_SKILLS_ALLOWED) takes away the
# agent's INSTRUCTIONS for installing skills. It does not take away the
# ability: `openclaw skills install` is a CLI, it stays in the image, and the
# agent's exec tool can call it. What actually stops an install is that its
# destination is root-owned and read-only (SKILLS-DIR-READONLY in
# k8s/020-deployment.yaml).
#
# So the lockdown is only as good as the list of directories it covers, and
# that list was partly guesswork: `openclaw skills --help` says installs go to
# "the active workspace skills/ directory" or, with --global, "the shared
# managed skills directory", and neither the help nor docs.openclaw.ai/cli/
# skills gives a path for the second one. This prints what the image itself
# knows, so the deployment locks paths that were read rather than assumed.
#
# Diagnostic only — the caller runs it with `|| true`. It must never fail a
# build; if openclaw changes its layout the right outcome is a human reading
# this output, not a red pipeline on a grep.
echo "=== openclaw skills — subcommands ==="
openclaw skills --help 2>&1 | head -30

echo
echo "=== openclaw skills install — destination flags ==="
openclaw skills install --help 2>&1 | head -25

echo
echo "=== skill paths the bundle resolves against ==="
# The loader and the installer build their directories from string literals in
# the bundle. Anything with a path-ish shape and 'skills' in it is a candidate
# destination the init container may need to lock.
grep -rohE '[A-Za-z0-9_.~/-]*[Ss]kills[A-Za-z0-9_.~/-]*' /app/dist 2>/dev/null \
  | grep -E '/|~' \
  | sort -u \
  | head -40

echo
echo "=== managed / shared / global variants specifically ==="
grep -rohE '[A-Za-z0-9_.~/-]*(managed|shared|global|Managed|Shared|Global)[A-Za-z0-9_.~/-]*[Ss]kills[A-Za-z0-9_.~/-]*' /app/dist 2>/dev/null \
  | sort -u \
  | head -25

echo
echo "=== chat slash commands: native, or skill-backed? ==="
# Stripping the bundled skills removes their slash commands too, because
# openclaw.json sets commands.nativeSkills = "auto" (bundled skills are
# offered as commands). Commands compiled into the binary are a different
# thing — commands.native = "auto" — and are unaffected. The distinction
# decides whether something like /models still answers in chat, so print
# both rather than reason about it from the names.
openclaw --help 2>&1 | head -80
echo
echo "--- is /models compiled in? (targeted, not a dump) ---"
# An earlier version dumped every "/foo" literal in the bundle. That is
# alphabetical and thousands long — full of OpenTelemetry and filesystem
# strings like /b3multi and /boot — and the answer to the only question
# being asked sat past the cut. Ask about the names that matter instead.
grep -rohE '"/(models?|help|status|compact|context|commands|agents?|config|approve|clear|new|resume|stop|usage|cost)"' /app/dist 2>/dev/null \
  | sort -u

echo
echo "=== what already exists under the state dir ==="
ls -la "$HOME/.openclaw" 2>/dev/null | head -20
echo "--- any skills dir on disk ---"
find / -maxdepth 6 -type d -name skills 2>/dev/null | head -20

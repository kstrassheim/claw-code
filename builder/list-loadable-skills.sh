#!/bin/sh
# Print the name of every skill that is LOADABLE in this image, one per line.
#
# WHY THIS IS A FILE AND NOT A `find` TYPED IN THREE PLACES
# The build strips unlisted skills, the build then asserts none survived, and
# CI re-checks the finished image from outside. All three have to agree on the
# same question — "what counts as a loadable skill?" — and every time that
# question has been answered separately, the answers have drifted:
#
#   - The strip once looked only in /app/skills while the assertion searched
#     the whole tree. Four skills shipped from extension bundles while the
#     build reported them all removed.
#   - The CI re-check once searched without excluding node_modules while the
#     assertion excluded it, so two npm DEPENDENCIES that happen to ship a
#     SKILL.md failed a build in which nothing was wrong.
#
# Both were the same bug: two implementations of one definition. So there is
# now one implementation, it ships in the image, and every caller runs it.
#
# THE DEFINITION
# A loadable skill is a directory `<...>/skills/<name>/` containing a
# SKILL.md, anywhere under the application root. That covers /app/skills, the
# skills bundled inside extensions at /app/extensions/<ext>/skills/<name>/,
# and their mirrors under /app/dist/ — openclaw loads all three.
#
# node_modules is excluded. A dependency shipping a SKILL.md is documentation
# or fixture data inside a package; openclaw does not load it as a skill, and
# matching there produces noise rather than findings.
#
# Prints nothing and exits 0 when there are none, which is the expected state
# for this image — the bot's own skills are mounted from reviewed ConfigMaps
# at runtime, not baked in.
set -eu

ROOT="${1:-/app}"

find "$ROOT" -name SKILL.md -not -path '*/node_modules/*' 2>/dev/null \
  | grep '/skills/' \
  | sed -E 's#.*/skills/([^/]+)/.*#\1#' \
  | sort -u

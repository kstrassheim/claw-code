# -- pull-request facts ------------------------------------------------

pr_head_sha() { # $1 = pr number
  head_sha_of_pr "$1"
}

# "<mergeable_state> <draft>" — e.g. "clean false", "dirty false", "blocked true".
#
# `mergeable_state` rather than `mergeable`: GitHub computes mergeability
# asynchronously and answers null while it is thinking, and reading null as
# "not mergeable" would make every freshly-pushed head look conflicted.
pr_merge_facts() { # $1 = pr number
  "${FORGE[@]}" change-request --number "$1" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    p = json.load(sys.stdin)
except Exception:
    print('unknown false'); raise SystemExit(0)
# Mergeability is three-valued on purpose: True, False, and None for 'the host
# has not worked it out yet'. Reading None as False makes every freshly-pushed
# head look conflicted, and the solver then sets about fixing a conflict that
# does not exist.
_m = p.get('mergeable')
print('%s %s' % ('clean' if _m is True else ('dirty' if _m is False else 'unknown'),
                 'true' if p.get('draft') else 'false'))
" 2>/dev/null
}


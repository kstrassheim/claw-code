"""Story points for the autonomous bot, in the unit that actually costs money:
model calls.

WHY CALLS AND NOT HOURS
-----------------------
The scale the operator set is anchored in time — 1 point ≈ 15 min, 2 ≈ 30 min,
3 ≈ 1 h, 5 ≈ 2 h, 8 ≈ 3 h — but wall-clock time is a poor measurement here. A
run can sit for twenty minutes waiting on a slot or on a provider that is
returning 429, and none of that is effort. Model calls are what the work
actually consumes, they are already logged per issue, and they are what the
weekly quota is spent on.

So the time anchors stay as the human-facing meaning of a point, and the cut
points below are what the estimator and the feedback loop compare against.

WHERE THE CUT POINTS COME FROM
------------------------------
Measured on 2026-08-03 over the bot's own history: 130 issues that saw real
work (≥3 model calls), counted per ISSUE — the sum over all of that issue's
runs, not per invocation. Counting invocations instead gives a median of 2,
because most invocations are preflight exits that do nothing; that number
describes the polling loop, not the work.

    calls per issue:  p10=15  p25=25  median=48  p75=156  p90=239  max=634

The bands below spread that distribution across the scale rather than piling
it on one value:

    1 SP    ≤ 15 calls    10.8% of issues
    2 SP    ≤ 30          21.5%
    3 SP    ≤ 60          26.9%
    5 SP    ≤ 120         12.3%
    8 SP    ≤ 240         19.2%
    13 SP   > 240          9.2%   -> too big, ask to split

Re-derive these when the model changes. A cheaper model that needs more turns
for the same work would shift every band, and a scale calibrated against Kimi
would then silently mis-size everything MiniMax does.
"""

from __future__ import annotations

import os
import re

# Fibonacci, as agreed. 13 exists only to name "too big"; the bot never
# commits to one — it asks for the story to be split.
SCALE = (1, 2, 3, 5, 8)
SPLIT_DEFAULT = 13

# WHERE THE CEILING IS READ FROM, and why it is not just a constant.
#
# 13 is a measurement (see above), not a law: it says "more than 240 model
# calls, which 9% of issues needed and most of those did not finish". A team
# that wants smaller stories — or a deployment on a model with a different
# cost per turn — needs to move it without a code change, exactly like the
# runtime limits.
#
# It is read from the SAME file the shell reads (agent-limits.conf, key
# `planning.split_points`) rather than from an environment variable. The
# estimator is shell, the reports are Python, and they must never disagree
# about what "too big" means: one of them would then park a story the other
# happily plans, and the numbers would stop adding up with nothing saying why.
#
# Read at import, which is once per process, and every runner is a fresh
# process — so a change applies to the next run without a restart.
_SETTINGS_FILE = os.environ.get(
    "AGENT_LIMITS_FILE",
    os.path.join(os.path.expanduser("~"), ".openclaw", "agent-limits.conf"))


def _configured_split() -> int | None:
    """The ceiling from the settings file — None when it is switched OFF.

    `agent-limits set planning.split_points off` stores a 0, and 0 here means
    "no ceiling": every size the scale offers is workable and nothing is ever
    handed back to be split. That is a real choice — a team that would rather
    have the bot attempt a large story and stop half-finished than be told to
    split it — so it is a setting and not something to be worked around by
    removing a label from every issue.

    Anything unreadable falls back rather than raising: a typo in the store
    must not stop the estimator, and the default is a number that is known to
    work. A value of 1 falls back too — it would make every story too big and
    nothing would ever be started again, which is not "off", it is "stop".
    """
    try:
        with open(_SETTINGS_FILE, encoding="utf-8") as fh:
            raw = ""
            for line in fh:
                line = line.split("#", 1)[0]
                m = re.match(r"\s*planning\.split_points\s*=\s*(\S+)", line)
                if m:
                    raw = m.group(1)      # last one wins, as the shell does
    except OSError:
        return SPLIT_DEFAULT
    text = raw.strip().lower()
    if text in ("off", "none", "disabled", "false", "0"):
        return None
    try:
        value = int(text)
    except ValueError:
        return SPLIT_DEFAULT
    if value < 2:
        return SPLIT_DEFAULT
    return value


_CONFIGURED = _configured_split()

# False = no story is ever too big. SPLIT_POINTS keeps a usable number even
# then, because the band arithmetic below needs one; nothing compares against
# it while this is False.
SPLIT_ENABLED = _CONFIGURED is not None
SPLIT_POINTS = _CONFIGURED if SPLIT_ENABLED else SPLIT_DEFAULT

# (points, inclusive upper bound in model calls)
BANDS = ((1, 15), (2, 30), (3, 60), (5, 120), (8, 240))

# The human-facing meaning of a point, for prose in issue comments. Not used
# for arithmetic — see the module docstring on why time is not the unit.
ROUGH_DURATION = {1: "15 min", 2: "30 min", 3: "1 h", 5: "2 h", 8: "3 h"}

# What an unestimated story is worth until someone or something estimates it.
#
# 8 and not 5, deliberately: an unknown story should get the strong model and
# the larger time budget, because under-estimating is the expensive direction
# — it is what makes a run die half-finished. A defaulted 8 and a judged 8 are
# NOT the same number, which is why every caller gets `defaulted` alongside it.
DEFAULT_POINTS = 8


def usable_scale() -> tuple[int, ...]:
    """The sizes the bot will actually commit to, given the ceiling.

    Everything at or above SPLIT_POINTS is "too big", so lowering the ceiling
    shortens the scale rather than merely renaming its top. With a ceiling of
    5 the usable sizes are 1, 2 and 3 — and a story someone weighted 4 rounds
    up to 5, which is then refused. That is the intended reading of a lower
    ceiling: smaller stories, more of them.
    """
    # No special case for the ceiling being OFF: it keeps SPLIT_POINTS at the
    # default 13, which is above every value in SCALE, so this returns the
    # whole scale by itself. A branch here would be one nothing can reach.
    return tuple(s for s in SCALE if s < SPLIT_POINTS)


def scale_table() -> str:
    """The scale as the estimator is shown it — bands, then the ceiling.

    Generated rather than written out, because it is shown to a MODEL and the
    model answers with a number from it. A table saying 13 next to a ceiling
    of 5 would produce estimates the wrapper then refuses, and the refusal
    would look like the model misbehaving rather than like two texts
    disagreeing.
    """
    what = {
        1: "a one-line or one-file change",
        2: "a small, well-understood change",
        3: "a contained feature or a clear bug fix",
        5: "several files, or an unclear cause",
        8: "a feature touching much of a component",
    }
    lines = []
    for points, upper in BANDS:
        if SPLIT_ENABLED and points >= SPLIT_POINTS:
            break
        lines.append(f"  {points} point{'s' if points > 1 else ' '}  "
                     f"up to {upper} model calls    {what.get(points, '')}")
    if SPLIT_ENABLED:
        lines.append(f"  {SPLIT_POINTS}        more than that          "
                     f"TOO BIG — must be split, not started")
    else:
        # No ceiling: the top band is the answer for anything larger. Saying
        # so beats leaving the model to guess what to write for a huge story
        # — it would reach for 13, which is not on the list it was given.
        lines.append(f"  Anything larger than that is still {SCALE[-1]}: "
                     f"there is no size above this scale here.")
    return "\n".join(lines)


def points_for_calls(calls: int) -> int:
    """The story points a piece of work actually needed, from its model calls.

    This is the measurement side of the loop: compare it against what was
    estimated to find out whether the estimator is drifting.
    """
    if calls is None or calls < 0:
        return DEFAULT_POINTS
    for points, upper in BANDS:
        if calls <= upper:
            return points
    return SPLIT_POINTS if SPLIT_ENABLED else SCALE[-1]


def calls_band(points: int) -> tuple[int, int]:
    """(lower, upper) model calls a story of this size is expected to take.

    Lower is exclusive, upper inclusive — the same convention the bands are
    written in. For SPLIT_POINTS the upper bound is unbounded, reported as -1.
    """
    lower = 0
    for p, upper in BANDS:
        if p == points:
            return (lower, upper)
        lower = upper
    return (BANDS[-1][1], -1)


def normalise(points) -> int | None:
    """Coerce a weight from the tracker onto the scale, or None.

    An issue will happily carry 4, 7 or 100 — a story-point label is a free
    string and nothing validates it. Anything off-scale is rounded UP to the
    next scale value rather than down: a story someone sized at 4 is closer in
    risk to a 5 than to a 3, and rounding down is the direction that starves a
    run.
    """
    try:
        v = int(str(points).strip())
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if SPLIT_ENABLED and v >= SPLIT_POINTS:
        return SPLIT_POINTS
    for s in SCALE:
        if v <= s:
            return s
    # Off the top of the scale. With a ceiling that IS the ceiling; without
    # one the story still needs a size to plan and route with, and the
    # largest the scale offers is the only honest answer.
    return SPLIT_POINTS if SPLIT_ENABLED else SCALE[-1]


def is_too_big(points) -> bool:
    """True when the story must be split instead of started.

    Never true when the ceiling is OFF, and that is handled in `normalise`
    rather than here: without a ceiling it clamps to the largest size on the
    scale, which is below the 13 this then compares against. Guarding it here
    as well would be a branch no test can reach — say where the answer comes
    from instead of writing it twice.
    """
    n = normalise(points)
    return n is not None and n >= SPLIT_POINTS


def effective(points) -> tuple[int, bool]:
    """(points, defaulted) — what to plan with, and whether it was a guess.

    `defaulted` must be carried into the sprint log. A sprint whose added
    scope is all defaulted 8s is not the same sprint as one whose additions
    were judged, and six weeks later nobody can tell them apart from the
    number alone.
    """
    n = normalise(points)
    if n is None:
        return (DEFAULT_POINTS, True)
    return (n, False)


def describe(points: int) -> str:
    """One line for an issue comment. States the basis, not just the number."""
    n = normalise(points)
    if n is None:
        return f"unestimated — planning with the default {DEFAULT_POINTS} points"
    # Same as is_too_big: with the ceiling off, `normalise` has already
    # clamped below this, so no check for the switch belongs here.
    if n >= SPLIT_POINTS:
        return (f"{SPLIT_POINTS}+ points: larger than this scale carries "
                f"(>{BANDS[-1][1]} model calls) — split it rather than start it")
    lo, hi = calls_band(n)
    return (f"{n} point(s) — roughly {ROUGH_DURATION.get(n, '?')} of model work, "
            f"expected {lo + 1}–{hi} model calls")


def drift(estimated, actual_calls: int) -> tuple[int, int, int]:
    """(estimated, required, delta) — the feedback loop, in points.

    `delta` positive means the work cost more than estimated. Feeding this
    back per story is what stops the estimator drifting; storing only the
    estimate means it never learns.
    """
    est, _ = effective(estimated)
    req = points_for_calls(actual_calls)
    return (est, req, req - est)

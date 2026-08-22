"""issue_priority: read a priority label off an issue or pull request.

Imported by heartbeat-issue-tick (issue solver) and reviewer-tick, so the two
cannot drift on what a priority label is — same reasoning as
project_allowlist.py and project-kind.sh.

FIVE LEVELS, JIRA-SHAPED
------------------------
    Very High · High · Medium · Low · Very Low

Matched case-insensitively, with or without a scope prefix (`Priority::High`
and `High` both work — a `scope::value` label is the usual way to make a set
mutually exclusive, though nothing here enforces that), and tolerant of the
separator people actually type:
`Very High`, `very-high`, `VeryHigh` all land on the same level.

**Default is Medium.** An issue with no priority label is not urgent and not
ignorable; it sits exactly in the middle, which is what makes adding labels
to a few items useful without having to label everything.

WHY IT SORTS, AND WHERE IT DOES NOT
-----------------------------------
Priority orders work *within* what the planner already decided is workable.
It does NOT override the in-flight rules: an issue whose review is running,
or whose MR is open, is still finished before a fresh one is started,
whatever their labels say. Those rules exist so the bot converges on one
issue at a time instead of leaving a trail of half-finished branches, and a
label should not be able to undo that.

Across projects it decides SPAWN ORDER. That matters because the subsystems
share one model-concurrency gate (/usr/local/bin/agent-slot): when slots are
scarce, whoever is spawned first gets one. Without that gate the ordering
would be cosmetic — with it, "Very High in project A before Medium in
project B" is real.
"""

import re

# Rank ascending = more urgent, so it can be used directly as a sort key.
LEVELS = {
    "veryhigh": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "verylow": 4,
}
DEFAULT_LEVEL = LEVELS["medium"]
NAMES = {v: k for k, v in LEVELS.items()}
DISPLAY = {0: "Very High", 1: "High", 2: "Medium", 3: "Low", 4: "Very Low"}

# Everything that is not a letter is noise: "Very High", "very-high",
# "very_high" and "VeryHigh" are the same instruction typed by four people.
_NOISE = re.compile(r"[^a-z]+")


def _normalise(label: str) -> str:
    """Label text -> comparison key. Drops any scope prefix, case and
    separators."""
    text = str(label or "")
    if "::" in text:
        text = text.split("::")[-1]
    return _NOISE.sub("", text.strip().lower())


def level_of_label(label: str) -> int | None:
    """The level a single label names, or None if it names none."""
    return LEVELS.get(_normalise(label))


def priority_of(labels) -> int:
    """The priority of an issue/MR from its labels.

    Takes the MOST urgent when several are present. Two priority labels on one
    item is a mistake, not an instruction — and resolving it downwards would
    quietly park work somebody explicitly marked urgent.
    """
    best = None
    for raw in labels or []:
        name = raw.get("name", "") if isinstance(raw, dict) else str(raw)
        lvl = level_of_label(name)
        if lvl is not None and (best is None or lvl < best):
            best = lvl
    return DEFAULT_LEVEL if best is None else best


def label_for(level: int) -> str:
    """Display name for a level — for logs and plan output."""
    return DISPLAY.get(level, DISPLAY[DEFAULT_LEVEL])

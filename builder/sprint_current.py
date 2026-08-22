"""Which sprint is running right now — answerable without reaching the store.

WHY A LOCAL MARKER AND NOT A QUERY
Every runner needs the sprint number: it goes on every work event, and an
event with no sprint is invisible to every report that matters. But querying
the planning store for it at the start of each run would make that store a
DEPENDENCY of the issue solver — one more thing whose outage stops work.

The store is deliberately built the other way round: writes spool locally and
flush when they can, so an outage makes reporting late rather than stopping the
bot. Reading the sprint number over the network would undo that in one line.

So the rollover writes a small marker on the workspace volume, and everything
else reads that. It survives a pod restart, it costs nothing, and it is
readable while the store does not exist at all — which is its state until one
is provisioned.

The marker is a CACHE OF A DECISION, not a second source of truth. The sprint
documents in the store remain authoritative; this says which one is current.
"""

from __future__ import annotations

import json
import os

MARKER = os.environ.get(
    "SPRINT_CURRENT_FILE",
    os.path.expanduser("~/.openclaw/sprint-current.json"))


def read() -> dict:
    """{number, startedAt, endsAt} — or {} when no sprint is recorded.

    Empty is a legitimate answer, not an error: before the first rollover, and
    on any environment where planning is not in use, there simply is no sprint.
    Callers must treat a missing number as "unassigned" rather than as a
    failure, or the planning feature becomes a prerequisite for solving issues.
    """
    try:
        with open(MARKER, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("number") is not None else {}
    except (OSError, ValueError):
        return {}


def number():
    """The active sprint number, or None."""
    return read().get("number")


def write(number, started_at: str, ends_at: str) -> bool:
    """Record the active sprint. Atomic — a runner may read it at any moment.

    Returns False rather than raising: failing to record the sprint must not
    fail the rollover itself. A missed marker means work events carry no
    sprint for a while, which is recoverable; a crashed rollover is not.
    """
    try:
        os.makedirs(os.path.dirname(MARKER), exist_ok=True)
        tmp = MARKER + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"number": number, "startedAt": started_at,
                       "endsAt": ends_at}, f)
        os.replace(tmp, MARKER)
        return True
    except OSError:
        return False


def clear() -> None:
    try:
        os.unlink(MARKER)
    except OSError:
        pass


def describe() -> str:
    d = read()
    if not d:
        return ("No sprint is recorded. Work is still tracked; it simply has "
                "no sprint attached until the first rollover.")
    return (f"Sprint {d['number']} — started {d.get('startedAt', '?')}, "
            f"ends {d.get('endsAt', '?')}")

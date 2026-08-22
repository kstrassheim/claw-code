"""Cross-subsystem queue visibility: does the bot still have issues to solve
or pull requests to review, anywhere?

WHY
---
"First solve and merge issues, then test" — the deployment tester is the most
expensive of the three subsystems (its run is the longest by a wide margin)
and the least urgent: re-testing a main commit while issues sit unassigned and
pull requests sit unreviewed spends the token budget on the wrong thing.

The shared slot gate cannot express that. It only knows who is running RIGHT
NOW, so a tester that wins the race starts anyway and then holds a slot for an
hour. The rule the operator asked for is about the QUEUES, across every
permitted project: the tester starts only when there is nothing left to solve
and nothing left to review.

HOW
---
The two planners already compute exactly these numbers on every tick, after
their own status gates (work-item status, On Hold parking, the project
allowlist). Re-deriving them in the tester would duplicate that logic and
drift from it — an issue parked On Hold awaiting a human reply would look like
pending work forever and silently disable testing for good.

So each planner publishes what it already knows into the main pod, and the
tester reads it. The planners run in CronJob pods and reach the main pod over
`kubectl exec`, which is why these are shell snippets rather than plain file
I/O.

STALENESS IS FAIL-OPEN, ON PURPOSE
----------------------------------
A marker older than the TTL is treated as "unknown", and unknown lets the
tester run. The alternative — no news means blocked — turns a suspended or
crashed planner into a permanent, silent shutdown of the deployment tester,
which is a worse failure than an occasional overlap: the overlap costs some
tokens, the shutdown costs every deploy going untested with nothing in the
logs to say why.
"""

from __future__ import annotations

import time

STATE_DIR = "$HOME/.openclaw/queue-state"

# Planners tick every 5 minutes; the tester every 10. Three missed planner
# ticks means something is actually wrong, not merely slow.
DEFAULT_TTL_SECONDS = 15 * 60

MARKER = "==QUEUES=="


def pod_write_snippet(name: str, count: int) -> str:
    """Shell to publish one queue depth into the main pod.

    Written atomically: the tester may read this file at any moment, and half
    a number is worse than a stale one.
    """
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    return (
        f"mkdir -p {STATE_DIR} 2>/dev/null || true; "
        f"printf '%s %s\\n' {int(count)} \"$(date +%s)\" > {STATE_DIR}/{safe}.tmp && "
        f"mv -f {STATE_DIR}/{safe}.tmp {STATE_DIR}/{safe} || true"
    )


def pod_read_snippet() -> str:
    """Shell emitting a MARKER section: one `name count timestamp` per line.

    Designed to ride along in an exec the caller is already making.
    """
    return (
        f'echo "{MARKER}"; '
        f"if [ -d {STATE_DIR} ]; then "
        f"  for f in $(find {STATE_DIR} -maxdepth 1 -mindepth 1 -type f "
        "        ! -name '*.tmp' 2>/dev/null); do "
        '    echo "$(basename "$f") $(cat "$f" 2>/dev/null)"; '
        "  done; "
        "fi; "
    )


def parse(lines: list[str]) -> dict[str, tuple[int, int]]:
    """`name -> (count, written_at)` from the MARKER section's lines."""
    out: dict[str, tuple[int, int]] = {}
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            continue
        name, count, ts = parts
        try:
            out[name] = (int(count), int(ts))
        except ValueError:
            continue
    return out


def blocking_reason(
    queues: dict[str, tuple[int, int]],
    ttl: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Why the tester must not start, or "" if it may.

    Only FRESH, NON-ZERO queues block. A missing or stale marker is unknown
    and does not block — see the module docstring on why that direction is the
    safe one.
    """
    now = time.time() if now is None else now
    busy = []
    for name, (count, ts) in sorted(queues.items()):
        if count <= 0:
            continue
        if now - ts > ttl:
            continue  # stale: we do not know, so we do not block
        busy.append(f"{count} {name}")
    if not busy:
        return ""
    return "solve and merge first: " + ", ".join(busy) + " still pending"

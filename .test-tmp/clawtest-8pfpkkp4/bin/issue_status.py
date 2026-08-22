"""Work-item status on GitHub, where the platform only has open and closed.

THE PROBLEM
-----------
The autonomous solver needs five distinct answers to "what is happening to this
issue?", because each one leads somewhere different on the next tick:

    To do        nobody has started; pick it up
    In progress  a run is in flight, or it is parked waiting on a person
    Done         delivered — a merged PR carried it
    Won't do     closed deliberately without delivering it
    Duplicate    a human's terminal call; never touch it again

GitHub issues carry exactly two states, `open` and `closed`. Collapsing five
answers into two loses the only distinctions the planner acts on: a closed
issue that was *delivered* must be recorded against a sprint, a closed issue
that was *revoked* must not, and an issue somebody else is already working is
not the same as one nobody has touched.

THE MAPPING, AND WHY IT SPLITS ACROSS TWO MECHANISMS
----------------------------------------------------
Non-terminal statuses live in a LABEL, terminal statuses live in GitHub's
NATIVE close reason:

    To do        open,   label absent (the default — see below)
    In progress  open,   label `status::in-progress`
    Done         closed, state_reason=completed
    Won't do     closed, state_reason=not_planned  + label `status::wont-do`
    Duplicate    closed, state_reason=not_planned  + label `status::duplicate`

Using `state_reason` for the terminal pair rather than a third label is not a
stylistic choice. It is the one place GitHub is genuinely BETTER than the
work-item model this replaces: there, closing an issue by any route sets
"Done", so a revoked issue and a delivered issue were indistinguishable
afterwards and delivery had to be re-derived from the merge history. GitHub
records the operator's intent at the moment of closing and never overwrites
it, so `completed` vs `not_planned` answers "was this delivered?" directly.
`not_planned` covers both Won't do and Duplicate, so a label separates those
two — and only those two.

WHY "To do" IS THE ABSENCE OF A LABEL
-------------------------------------
Every issue that exists without this system having touched it is, correctly,
To do. Encoding that as a label would mean the bot has to label every issue in
every permitted repo before it can plan, which is a write against issues it
may never work on. Absence is the safer default in the other direction too: a
label the bot fails to write leaves the issue pickable rather than stranded.

MUTUAL EXCLUSION IS THIS MODULE'S JOB
-------------------------------------
Some forges enforce one-value-per-scope for `scope::value` labels natively.
GitHub does not — `status::in-progress` and `status::wont-do` can sit on the
same issue quite happily, and then "what is the status?" has two answers.
`label_updates()` therefore always returns the removals alongside the
addition, and every caller must apply both. Reading tolerates the broken state
anyway (`status_of` resolves by precedence) because refusing to answer would
wedge the planner on an issue a human mislabelled by hand.

WRITE ONLY ON CHANGE
--------------------
A label write that changes nothing still appends a timeline event, and this
runs every five minutes. `label_updates()` returns empty sets when the issue
already says what we were about to say, so an unchanged issue stays quiet
instead of accumulating a heartbeat log no human can read past.
"""

from __future__ import annotations

# Canonical status names. Lower-case internally; the display forms are what a
# human sees in a label or a comment.
TO_DO = "to do"
IN_PROGRESS = "in progress"
DONE = "done"
WONT_DO = "won't do"
DUPLICATE = "duplicate"

STATUSES = (TO_DO, IN_PROGRESS, DONE, WONT_DO, DUPLICATE)

# The statuses a planner may pick up. Anything else is either finished or
# somebody else's call.
WORKABLE = frozenset({TO_DO, IN_PROGRESS})

# Terminal statuses close the issue.
TERMINAL = frozenset({DONE, WONT_DO, DUPLICATE})

LABEL_PREFIX = "status::"

# The label written for each status. `To do` and `Done` deliberately have
# none: To do is the default, and Done is fully described by
# state_reason=completed, so a label would be a second source of truth that
# can disagree with the first.
_LABELS = {
    IN_PROGRESS: "status::in-progress",
    WONT_DO: "status::wont-do",
    DUPLICATE: "status::duplicate",
}

# Spellings accepted when READING a label, so a human typing the obvious thing
# by hand is understood. Keys are the normalised form (see `_key`).
_ALIASES = {
    "todo": TO_DO,
    "to do": TO_DO,
    "open": TO_DO,
    "inprogress": IN_PROGRESS,
    "in progress": IN_PROGRESS,
    "wip": IN_PROGRESS,
    "doing": IN_PROGRESS,
    "done": DONE,
    "delivered": DONE,
    "wontdo": WONT_DO,
    "won t do": WONT_DO,
    "wont fix": WONT_DO,
    "wontfix": WONT_DO,
    "duplicate": DUPLICATE,
    "dupe": DUPLICATE,
}

# When an issue carries contradictory status labels, the most advanced one
# wins. Reading must always produce an answer: a planner that raises on a
# mislabelled issue stops planning every OTHER issue in the same tick.
_PRECEDENCE = (DUPLICATE, WONT_DO, DONE, IN_PROGRESS, TO_DO)

# GitHub's native close reasons.
COMPLETED = "completed"
NOT_PLANNED = "not_planned"


def _key(name: str) -> str:
    """Normalise a label for comparison: drop the scope, fold punctuation.

    Tolerant on purpose. `Status::In-Progress`, `status::in progress` and a
    bare `WIP` all mean the same thing to a person, so they mean the same
    thing here. Apostrophes go because "won't do" and "wont do" are the same
    word and only one of them is easy to type into a label box.
    """
    text = str(name or "").strip().lower()
    if "::" in text:
        text = text.rsplit("::", 1)[-1]
    text = text.replace("'", "").replace("’", "")
    out = []
    for ch in text:
        out.append(ch if ch.isalnum() else " ")
    return " ".join("".join(out).split())


def normalize(name: str) -> str | None:
    """Resolve any spelling of a status to its canonical name, or None."""
    key = _key(name)
    if not key:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    # Fall back to a direct match against the canonical names themselves,
    # normalised the same way ("won't do" -> "wont do").
    for status in STATUSES:
        if _key(status) == key:
            return status
    return None


def is_status_label(name: str) -> bool:
    """True for a label this module owns.

    Prefix-based, so a `status::` label naming something we do not recognise
    is still ours to clear. Otherwise a typo'd label would survive every
    transition and permanently contradict the real status.
    """
    return str(name or "").strip().lower().startswith(LABEL_PREFIX)


def label_for(status: str) -> str | None:
    """The label that encodes a status, or None when it needs no label."""
    return _LABELS.get(normalize(status) or "")


def status_of(labels, *, state: str = "open",
              state_reason: str | None = None) -> str:
    """The status of an issue, from its labels and its GitHub state.

    `state`/`state_reason` win over labels for a closed issue: the close
    reason is what the person who closed it actually chose, whereas a stale
    `status::in-progress` label is just something nobody cleaned up. An issue
    closed while a run was in flight is the normal way that happens.
    """
    names = [str(l) for l in (labels or [])]

    if str(state).lower() == "closed":
        if str(state_reason or "").lower() == NOT_PLANNED:
            # not_planned covers both; the label is the only thing that
            # separates a duplicate from a refusal.
            for name in names:
                if normalize(name) == DUPLICATE and is_status_label(name):
                    return DUPLICATE
            return WONT_DO
        # `completed`, or an older issue closed before state_reason existed.
        return DONE

    found = set()
    for name in names:
        if not is_status_label(name):
            continue
        resolved = normalize(name)
        if resolved:
            found.add(resolved)
    for status in _PRECEDENCE:
        if status in found:
            return status
    return TO_DO


# How a close is recorded, in intent rather than in any host's spelling. The
# two are what a caller actually knows: the work shipped, or it was called
# off. Which field or label a code host writes that into is the forge's
# business — see forge.py.
DELIVERED = "delivered"
REVOKED = "revoked"


def status_of_item(labels, *, state: str = "open",
                   closed_as: str | None = None) -> str:
    """`status_of` for a work item in the bot's own vocabulary.

    The planners never see a host's native close reason — they see whether the
    work was DELIVERED or REVOKED — so this is the entry point they use, and
    `status_of` stays as the one that speaks the field directly for readers
    that already hold one.

    `closed_as=None` on a closed item means the host recorded no intent at
    all, which every such item predates the distinction: those were
    deliveries, and that is what `status_of` has always said about them.
    """
    reason = None
    if closed_as == REVOKED:
        reason = NOT_PLANNED
    elif closed_as == DELIVERED:
        reason = COMPLETED
    return status_of(labels, state=state, state_reason=reason)


def is_workable(status: str) -> bool:
    """True when a planner may pick this issue up."""
    return (normalize(status) or TO_DO) in WORKABLE


def close_reason(status: str) -> str | None:
    """The GitHub `state_reason` for a terminal status, else None."""
    resolved = normalize(status)
    if resolved == DONE:
        return COMPLETED
    if resolved in (WONT_DO, DUPLICATE):
        return NOT_PLANNED
    return None


def label_updates(current, status: str) -> tuple[list[str], list[str]]:
    """(labels to add, labels to remove) to put an issue into `status`.

    Returns two empty lists when the issue already carries exactly the right
    status labels — the caller then skips the API call entirely, which is what
    keeps a five-minute tick from writing a timeline event every time it looks
    at an issue it is not changing.

    Removals cover every `status::` label that is not the wanted one, so this
    also repairs an issue that somehow ended up with two.
    """
    resolved = normalize(status)
    if resolved is None:
        raise ValueError(f"unknown status: {status!r}")

    wanted = _LABELS.get(resolved)
    have = [str(l) for l in (current or [])]

    remove = [name for name in have
              if is_status_label(name) and (wanted is None or name != wanted)]
    add = []
    if wanted is not None and not any(name == wanted for name in have):
        add.append(wanted)
    return add, remove


def label_definitions() -> list[dict]:
    """The labels this model needs, for creating them in a fresh repo.

    Colours are the muted end of the palette on purpose: status is context for
    reading the issue list, not the thing a human should be drawn to first.
    """
    return [
        {"name": _LABELS[IN_PROGRESS], "color": "1d76db",
         "description": "A run is in flight, or the issue is parked waiting on a person."},
        {"name": _LABELS[WONT_DO], "color": "6a737d",
         "description": "Closed deliberately without delivering it."},
        {"name": _LABELS[DUPLICATE], "color": "6a737d",
         "description": "Superseded by another issue. The bot never sets or clears this."},
    ]

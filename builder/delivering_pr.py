"""Which pull request actually DELIVERED an issue — if any did.

WHAT THIS IS FOR
The solver marks a story delivered by writing `mergedAt` on its planning
document. Every report downstream is built on that field: completed points,
velocity, the burndown, the sprint's scope accounting, and "was this story
re-worked after it shipped". A wrong `mergedAt` is not a wrong number in one
report — it is a wrong number in all of them, and it does not look wrong.

WHY THIS IS NOT A ONE-LINE LOOKUP
The obvious implementation is "ask the platform which pull requests are
related to this issue, take the last merged one". Both halves are wrong.

"Related" is a LOOSER question than the one being asked. Anything that so
much as mentions the issue number is related to it: a comment referencing it,
a branch that happens to carry the number, a pull request that says "unlike
#63, this one…". Mentioning is not closing, and a report built on mentions
records deliveries that never happened.

"The last one" is not the newest one. Ordering is not guaranteed to be
chronological, so taking the final element of a list is a coin flip that
looks like a rule. When it lands wrong it does not fail — it writes a
confident, plausible, wrong timestamp.

The concrete shape of the failure: an issue whose real pull request is still
OPEN, alongside three merged ones that merely mention it, one of them merged
eighteen months before the issue was even created. The naive rule records the
story as delivered, with the delivery predating the issue, while the work is
still in progress. Read back out of the store, the story looks like it was
re-processed long after shipping — so the anomaly it produces is not even the
anomaly it actually is, and the trail leads nowhere.

THE RULE
A pull request delivered this issue only if:

  - it is the branch the runner works the issue on, which is the ordinary
    case and needs no text at all; or
  - it says so with a CLOSING KEYWORD — the platform's own vocabulary, since
    that is what actually makes it close the issue on merge.

And when more than one qualifies, the NEWEST MERGE wins.

The keyword list is the platform's, exactly, and no wider. `implement` and
`address` read like delivery to a person and are ignored by the platform, so
honouring them here would record a delivery that did not happen — the precise
error this module exists to prevent.
"""

from __future__ import annotations

import datetime
import re


def _ts(value):
    """A timestamp as a comparable value, or None.

    Not string comparison. Timestamps arrive both as '…Z' and as '…+02:00',
    and the two orderings disagree: 14:30+02:00 IS 12:30Z, earlier than
    13:00Z, but as text it sorts after it. A repository on a +01:00/+02:00
    offset would therefore compare wrongly in one place and correctly in
    another — the worst way for a comparison to be wrong, because CI would
    stay green.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


# The platform's closing vocabulary, and only it: close/closes/closed/closing,
# fix/fixes/fixed/fixing, resolve/resolves/resolved/resolving. Deliberately
# NOT `implement` or `address` — see the module docstring.
_KEYWORD = re.compile(
    r"\b(?:clos(?:e|es|ed|ing)"
    r"|fix(?:|es|ed|ing)"
    r"|resolv(?:e|es|ed|ing))\b:?",
    re.IGNORECASE)

# What may follow a keyword and still be part of the same closing statement:
# "#63", "issue #63", ", #64", "and #65", and the cross-repository
# "owner/repo#63" form. Anchored with .match() at the point the previous
# reference ended, so the list is CONSUMED rather than searched — that is what
# stops "Closes the gap … discussed in #63" from reading as a closing
# reference, and what lets "Closes #63, #64" close both.
_REFERENCE = re.compile(
    r"\s*(?:and\s+)?,?\s*(?:issues?\s+)?"
    r"(?:(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+))?#(?P<number>\d+)",
    re.IGNORECASE)


def closed_issues(body, repo: str = "") -> set:
    """Every issue number this body actually CLOSES, as strings.

    `repo` is this pull request's own `owner/name`. A cross-repository
    reference (`other/project#12`) closes an issue somewhere else and must not
    be read as closing number 12 here — without that check, two repositories
    that both have an issue 12 cross-contaminate each other's delivery
    records.
    """
    text = str(body or "")
    found = set()
    for keyword in _KEYWORD.finditer(text):
        position = keyword.end()
        while True:
            reference = _REFERENCE.match(text, position)
            if not reference:
                break
            named = reference.group("repo")
            if not named or (repo and named.casefold() == repo.casefold()):
                found.add(reference.group("number"))
            position = reference.end()
    return found


def is_merged(pr) -> bool:
    """True when this pull request was actually merged.

    A merged pull request is `closed` with a `merged_at`. Reading `state`
    alone counts every ABANDONED pull request as a delivery, which is the
    same class of error as counting mentions as closures.
    """
    if not isinstance(pr, dict):
        return False
    if pr.get("merged") is True:
        return True
    return bool(pr.get("merged_at"))


def closes(pr, number, branch: str = "", repo: str = "") -> bool:
    """Does this pull request close issue `number`?"""
    if not isinstance(pr, dict):
        return False
    if branch:
        head = pr.get("head") or {}
        head_ref = head.get("ref") if isinstance(head, dict) else None
        if (head_ref or pr.get("head_ref")) == branch:
            return True
    return str(number) in closed_issues(pr.get("body"), repo=repo)


def pick(prs, number, branch: str = "", not_before: str = "", repo: str = ""):
    """The pull request that delivered issue `number`, or None.

    `not_before` is the issue's creation time, when the caller has it. A merge
    that predates the issue cannot have delivered it, and this is the one
    check that does not depend on anybody having written the reference
    correctly. Omitted rather than guessed when the caller has no creation
    time: a missing guard is better than one that silently drops real
    deliveries because a clock disagreed.
    """
    candidates = [p for p in (prs or [])
                  if isinstance(p, dict)
                  and is_merged(p)
                  and closes(p, number, branch, repo=repo)]
    floor = _ts(not_before)
    if floor is not None:
        # An unparseable or absent merge time is KEPT. The guard exists to
        # throw out a merge that provably predates the issue; a timestamp it
        # cannot read proves nothing, and dropping the story on that would
        # lose a real delivery to a formatting change.
        candidates = [p for p in candidates
                      if _ts(p.get("merged_at")) is None
                      or _ts(p["merged_at"]) >= floor]
    if not candidates:
        return None
    epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return max(candidates, key=lambda p: _ts(p.get("merged_at")) or epoch)

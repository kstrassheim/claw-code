"""What a review was ABOUT — not merely which commit it looked at.

WHY THIS EXISTS
The autonomous reviewer judges a pull request, and a pull request is more than
its diff. It reviews the description too: whether the pull request claims to
close an issue it only half-fixes, whether the stated scope matches the change.
Those verdicts are real, and they are answered by editing prose, not by pushing
a commit.

Keyed on the head SHA alone, everything downstream broke on exactly that case:
the planner skipped a pull request whose SHA it had already reviewed, the runner
recorded only that SHA, and the solver's verdict fingerprint was `verdict:sha`.
So a CHANGES REQUIRED verdict about the description could never be cleared. The
reviewer asks for a `Closes #5` line to go, the author removes it within
minutes, and nothing looks again — the solver starts and exits on every tick,
zero model calls, until its retry budget runs out and it asks for a human. The
author did the right thing and it counted for nothing.

So the record of "I reviewed this" carries the SHA *and* a digest of the prose
that was reviewed. Either moving means there is something new to look at.

ONE MODULE, TWO CALLERS, BECAUSE THEY MUST AGREE
The reader is Python (reviewer-tick.py, in the cron pod) and the writer is
shell (reviewer-runner.sh, in the openclaw pod). If the two computed this
differently by so much as a strip(), every pull request would look changed
forever and the reviewer would re-review on a loop.
"""

from __future__ import annotations

import hashlib

# Long enough that a collision is not a thing that happens, short enough that
# the state file stays readable by a person debugging it at 2am.
DIGEST_LEN = 12


def fingerprint(title: str | None, description: str | None) -> str:
    """A stable digest of the prose a reviewer judges.

    Line endings are normalised and the ends are stripped: the GitHub web
    editor and the API disagree about trailing newlines and CRLF, and a review
    must not be re-run because something invisible moved.
    """
    h = hashlib.sha256()
    for part in (title, description):
        text = (part or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        h.update(text.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:DIGEST_LEN]


def stamp(sha: str, title: str | None, description: str | None) -> str:
    """The line to record after a review: what was reviewed, in one string."""
    return f"{sha} {fingerprint(title, description)}"


def parse(stored: str | None) -> tuple[str, str]:
    """A recorded stamp as (sha, prose_digest). Digest is "" when unknown.

    Unknown covers the state files written before this existed, which hold a
    bare SHA. It is not the same as "no prose" and must not compare equal to
    the digest of an empty description.
    """
    parts = (stored or "").split()
    if not parts:
        return ("", "")
    return (parts[0], parts[1] if len(parts) > 1 else "")


def already_reviewed(stored: str | None, sha: str,
                     title: str | None, description: str | None) -> bool:
    """True ⟺ this exact pull request — same commit AND same prose — has
    already been reviewed, so there is nothing new to look at.

    Fails towards REVIEWING (False) in every uncertain case. A needless review
    costs one run; a skipped one strands the pull request forever, which is the
    failure this module was written for.

    A legacy bare-SHA record therefore earns exactly one re-review, after which
    the stamp carries its prose digest and the question stops arising.
    """
    if not sha:
        return False
    recorded_sha, recorded_prose = parse(stored)
    if recorded_sha != sha:
        return False
    if not recorded_prose:
        return False
    return recorded_prose == fingerprint(title, description)

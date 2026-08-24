"""When a MERGE APPROVAL park may be lifted — one definition, two callers.

Two processes decide this and they must not be able to disagree.  The
planner (`heartbeat-issue-tick.release_hold`) takes the `On Hold` label off;
the solver's `approval_gate` decides whether the merge may proceed.  If the
planner released on something the gate then refuses, the issue is handed to a
runner that re-asks and re-parks it, every tick, forever — a comment loop with
no exit.  So both import this.

WHY THE RELEASE RULE DEPENDS ON WHY IT IS PARKED.
`On Hold` means one thing to a reader — "waiting on a person" — but the act
that ends the wait is not the same act in both cases:

  * the bot ASKED A QUESTION (lexical guard, CI-red escalation).  Only an
    answer ends it, and the answer has to be addressed to the bot: a reply
    that @-mentions it.  Bystander chatter on a destructive-sounding issue is
    not a go-ahead, which is why that bar stays deliberately high.

  * the bot NEEDS A SIGN-OFF on a pull request.  The act that ends *this* wait
    is approving the pull request.  Demanding an @-mention as well would mean
    the reviewer approves, nothing happens, and the only symptom is silence —
    the deadlock this module exists to prevent.

Which kind a park is, is readable off the issue itself: the bot's own ask note
says `MERGE APPROVAL REQUESTED`.  No extra state, and nothing to get out of
sync with the labels.
"""

import re

# The ask `fixer-runner.sh:request_merge_approval` posts.  Matched loosely on
# the headline alone: the body around it is prose and will be reworded, but
# the headline is the marker and is not.
_ASK = re.compile(r"merge\s+approval\s+requested", re.I)
_SHA = re.compile(r"sha\s*`([0-9a-f]{7,40})`", re.I)
_PR = re.compile(r"\bPR\s*#(\d+)")

# The notice `fixer-runner.sh:park_merge_blocked` posts when the host refused
# the merge itself. A different wait from the one above — the sign-off is
# already on record and what is missing is the button press — so it is
# released by a different act, and has to be told apart from it.
_BLOCKED = re.compile(r"merge\s+blocked", re.I)

# A sign-off typed as prose rather than clicked.
#
# Read only against `_prose` below, never the raw body. A merge is not
# reversible by the person who did not ask for it, so the cost of matching
# "lgtm" inside a pasted link, a quoted diff or a code block is not symmetric
# with the cost of missing a real one — the reviewer who is ignored says so
# again, the reviewer who is misread finds unreviewed code on the branch.
_LGTM = re.compile(r"\b(lgtm|ship\s*it|looks\s+good\s+to\s+me)\b", re.I)

_FENCE = re.compile(r"```.*?```", re.S)
_TICKS = re.compile(r"`[^`]*`")
_LINK = re.compile(r"\S+://\S+")
_QUOTE = re.compile(r"^\s*>.*$", re.M)


def _prose(body) -> str:
    """A comment with everything that is not the author speaking removed.

    Fenced blocks, inline code, links and quoted lines: all of them can carry
    the word "lgtm" without anybody having said it. Stripped in that order so
    a link inside a code span does not survive by being handled twice.
    """
    text = str(body or "")
    for pattern in (_FENCE, _TICKS, _QUOTE, _LINK):
        text = pattern.sub(" ", text)
    return text


def _norm(text) -> str:
    return str(text or "").strip().lower()


def _login(note) -> str:
    """The login on a note, whichever shape the forge returned it in.

    GitHub nests it under `author.login`, GitLab under `author.username`, and
    a review verdict carries it as a bare string — three shapes for one fact,
    unwrapped once here so no caller has to know which host answered.
    """
    if not isinstance(note, dict):
        return ""
    who = note.get("author")
    if isinstance(who, dict):
        who = who.get("username") or who.get("login")
    return str(who or "").strip()


def _author(note) -> str:
    """`_login`, folded for comparison against a bot name."""
    return _norm(_login(note))


def sha_match(a, b) -> bool:
    """True when two shas name the same commit, one possibly abbreviated.

    The ask quotes 8 characters; a review verdict carries all 40.  Comparing
    them with `==` — which is what the solver used to do — can only ever be
    false for an abbreviated side, so an approval would never be recognised.
    """
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


def _newest(comments, bot, pattern) -> dict | None:
    """The newest note the bot posted whose body matches `pattern`."""
    bot = _norm(bot)
    best = None
    for note in comments or []:
        if not isinstance(note, dict) or note.get("system"):
            continue
        if not bot or _author(note) != bot:
            continue
        body = str(note.get("body") or "")
        if not pattern.search(body):
            continue
        try:
            nid = int(note.get("id"))
        except (TypeError, ValueError):
            continue
        if best is not None and nid <= best["id"]:
            continue
        pr = _PR.search(body)
        sha = _SHA.search(body)
        best = {"id": nid,
                "pr": int(pr.group(1)) if pr else None,
                "sha": sha.group(1) if sha else ""}
    return best


def approval_ask(comments, bot) -> dict | None:
    """The newest merge-approval ask the BOT posted, or None.

    Returns `{"id": note_id, "pr": int|None, "sha": str}`.  Its presence is
    what makes a park an approval park; its id is the anchor a sign-off has to
    come after, so that an `LGTM` left before the bot ever asked — or left for
    an earlier commit, since a push re-asks — is not read as one.
    """
    return _newest(comments, bot, _ASK)


def merge_blocked_ask(comments, bot) -> dict | None:
    """The newest merge-blocked notice the BOT posted, or None.

    Same shape as `approval_ask`. What ends THIS wait is the pull request
    actually landing — the person merges it by hand — so the planner checks
    the change request's state rather than looking for a reply.
    """
    return _newest(comments, bot, _BLOCKED)


def newest_park_ask(comments, bot) -> tuple[str, dict] | tuple[None, None]:
    """Which kind of park the bot's newest ask is: ("approval"|"blocked", ask).

    A solver asks more than one kind of question over an issue's life, and
    only the LAST one is the wait that is still open. Picking the newest is
    what stops a sign-off given weeks ago from releasing a park that a later
    escalation put there.
    """
    kinds = {"approval": approval_ask(comments, bot),
             "blocked": merge_blocked_ask(comments, bot)}
    best_kind, best = None, None
    for kind, ask in kinds.items():
        if ask is None:
            continue
        if best is None or ask["id"] > best["id"]:
            best_kind, best = kind, ask
    return best_kind, best


def changes_requested(verdicts=None, bot="", sha="") -> dict | None:
    """Who rejected `sha`, or None. A rejection is WORK, not a wait.

    The park exists because the bot is waiting on a person. A reviewer who
    presses "Request changes" has STOPPED being waited on — they have handed
    the issue back — so the park has to lift on this exactly as it lifts on an
    approval, and for the same reason: the alternative is the reviewer typing
    what they want and the bot never reading it.

    Deliberately not merged into `signed_off`. They release the same park and
    mean opposite things: one lets the merge proceed, the other sends the
    solver back to work, and a caller that could not tell them apart would
    merge on a rejection.

    GITHUB ONLY, structurally: GitLab has no "changes requested" state — a
    rejection there travels as an ordinary note (see
    `forge.GitLab.review_verdicts`) and is read through the @-mention path
    the ask names. Nothing here is host-specific; there is simply nothing to
    match on the other host.
    """
    bot = _norm(bot)
    for r in reversed(list(verdicts or [])):
        if not isinstance(r, dict):
            continue
        who = _norm(r.get("author"))
        if not who or who == bot:
            continue
        if _norm(r.get("verdict")) != "changes_requested":
            continue
        if sha and not sha_match(sha, r.get("sha")):
            continue      # rejected a commit that has since been replaced
        return {"who": r.get("author"), "how": "requested changes"}
    return None


def signed_off(verdicts=None, comments=None, bot="", sha="",
               anchor_id=None) -> dict | None:
    """Who signed `sha` off and how, or None if nobody has.

    Two ways, because a reviewer may use either and both are unambiguous:

      * they approved the pull request — the button, and the primary path;
      * they said so in words after the ask, for anyone who answers on the
        issue rather than in the review UI.

    Fails toward NOT released.  Anything unreadable here leaves the issue
    parked, which costs a reply; the opposite mistake merges code nobody
    approved.
    """
    bot = _norm(bot)

    # The button first: it carries a sha, so it cannot be mistaken for a
    # sign-off on a commit that has since been replaced.
    for r in reversed(list(verdicts or [])):
        if not isinstance(r, dict):
            continue
        who = _norm(r.get("author"))
        if not who or who == bot:
            continue
        if _norm(r.get("verdict")) != "approved":
            continue
        if sha and not sha_match(sha, r.get("sha")):
            continue      # approved an older commit; a push invalidates it
        return {"who": r.get("author"), "how": "approved the pull request"}

    # A sign-off typed into the review UI as a plain Comment review. The
    # verdict state is "commented", so the button path above skips it — and the
    # body never reaches the comments list, because the host keeps review
    # bodies in a separate API. Observed: the reviewer answered the ask with a
    # review whose whole body was "lgtm", and the park held for hours with the
    # approval sitting in plain sight — exactly the silence this module exists
    # to prevent. The verdict carries the sha it was given on, which anchors
    # it to the commit the ask named; one without a sha stays unread, failing
    # toward parked like everything else here.
    for r in reversed(list(verdicts or [])):
        if not isinstance(r, dict):
            continue
        who = _norm(r.get("author"))
        if not who or who == bot:
            continue
        if not r.get("sha") or (sha and not sha_match(sha, r.get("sha"))):
            continue
        if _LGTM.search(_prose(r.get("body"))):
            return {"who": r.get("author"), "how": "signed off in a review comment"}

    # Words, which carry no sha — so the anchor does that job instead. The ask
    # is re-posted for every new head commit, so "after the newest ask" means
    # "about the commit the ask named".
    if anchor_id is None:
        return None
    for note in comments or []:
        if not isinstance(note, dict) or note.get("system"):
            continue
        who = _author(note)
        if not who or who == bot:
            continue
        try:
            nid = int(note.get("id"))
        except (TypeError, ValueError):
            continue
        if nid <= anchor_id:
            continue
        if _LGTM.search(_prose(note.get("body"))):
            return {"who": _login(note), "how": "said so on the issue"}
    return None

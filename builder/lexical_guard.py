"""Does this issue ASK FOR something destructive — and should a human confirm?

WHAT THIS IS FOR
Some issues ask, in passing, for the thing you least want an autonomous agent
to do on its own: delete the tests, disable auth, drop the monitoring. The
wording is usually innocent ("remove the flaky tests so CI is green"), and the
agent is perfectly capable of doing exactly what was asked. So before any code
is written, a destructive-sounding request is put back to a human.

WHY IT IS A MODULE AND NOT TWENTY LINES OF SHELL
It was twenty lines of shell — embedded Python inside a double-quoted string
inside the issue-solver runner, which is both untestable and a live quoting
hazard (a double quote in a comment there closes the surrounding string and
bash parses the Python as shell; that has happened). It now has a second
caller, the planner, and a rule with two copies is a rule with two behaviours.

THE THREE WAYS THIS GETS IT WRONG, ALL OF THEM SILENT
  - matching inside CODE. An identifier is a name, not an instruction:
    `Remove-AzRoleAssignment` is the API you must not call, `DROP TABLE` is
    ordinary DDL in a warehouse. Observed on claw-code-automation-test #2 and
    #5, where acceptance criteria FORBIDDING a call ("contains no
    `Remove-AzRoleAssignment`") tripped the guard into asking whether to make
    it. Code spans are blanked before matching, with spaces, so the offsets
    and therefore the proximity window stay honest.
  - reading a PROHIBITION as a request. "must not delete", "without
    disabling", "contains no remove" are the issue telling you NOT to.
  - firing on two words that merely co-occur in a long document. Hence the
    120-character proximity window rather than "both appear somewhere".

A false positive costs a round trip with a human. A false negative deletes
somebody's test suite. The bias is deliberate and it is towards asking.
"""

from __future__ import annotations

import re

# The first line of the note the bot posts. Both the planner and the solver
# key on it to decide whether the question has ALREADY been asked, so it must
# not change casually — an edit here re-asks on every issue mid-flight.
ASK_MARKER = "DESTRUCTIVE CHANGE — PLEASE CONFIRM"

_DESTRUCTIVE = (
    r"\b(?:remove|delete|disable|drop|strip|kill|turn\s*off|get\s*rid\s*of"
    # Verbs that only ever appear against infrastructure and data, added with
    # the nouns below. `destroy` in particular is the word terraform itself
    # uses, so an issue asking for exactly the irreversible thing was phrased
    # in the tool's own vocabulary and matched nothing.
    r"|destroy|tear\s*down|wipe|purge|truncate|deprovision|decommission"
    r"|scale\s*(?:down\s*)?to\s*zero)\b")

# A destructive verb that is NEGATED is a prohibition, not a request. Anchored
# at the end so it only looks at the text immediately BEFORE the verb.
_NEGATION = re.compile(
    r"(?:\b(?:no|not|never|without|avoid|forbid(?:den|s)?|must\s+not|do\s+not"
    r"|does\s+not|don'?t|cannot|can'?t|refuse[sd]?)\b|\bNO\b)[\s\S]{0,40}$",
    re.IGNORECASE)

# WHAT COUNTS AS LOAD-BEARING, and why the list grew.
#
# Every original entry protects a QUALITY GATE or an OBSERVABILITY signal:
# undoing one costs confidence, and the fix is to put it back. The second
# group is different in kind — infrastructure and data — and undoing one of
# those is not recoverable by re-running anything. A dropped table is gone; a
# destroyed volume is gone; a deleted namespace takes everything in it.
#
# The gap was found on a real issue that the guard let straight through:
#
#     "Drop the staging namespace and delete its volumes"
#
# Three destructive verbs, and not one protected noun, so it was planned as
# ordinary work. The verbs were never the problem.
_PROTECTED = (
    r"\b(?:tests?|test\s+suite|test\s+files?|snapshots?|jest|lint|eslint"
    r"|prettier|type[-\s]?check|tsconfig\b[^.]*?strict|mypy[^.]*?strict"
    r"|ci\s+jobs?|pipelines?|workflows?|coverage|monitor(?:ing)?|logging"
    r"|tracking|security|auth(?:entication)?|authorization|backups?"
    r"|rollbacks?"
    # Infrastructure — but only what a re-apply does NOT bring back.
    #
    # THE LINE IS RECOVERABILITY, not "is it infrastructure". A Deployment,
    # StatefulSet, Ingress or Certificate is declarative: delete one and Argo
    # recreates it from git on the next sync. Those are deliberately absent —
    # and they are the words that appear in nearly every issue title in a
    # Kubernetes repository, so protecting them parks routine work. A
    # namespace takes its contents with it, a volume takes its data, a cluster
    # takes both, and nothing in git brings any of it back.
    r"|namespaces?|clusters?|nodes?|node\s*pools?|volumes?|pvcs?"
    r"|persistent\s*volumes?|disks?|storage\s*accounts?|resource\s*groups?"
    r"|key\s*vaults?"
    # State a tool owns and rebuilds from: losing it orphans real resources.
    r"|terraform\s*state|tfstate|state\s*files?|lock\s*files?"
    # Data: the entries no re-run brings back.
    r"|databases?|schemas?|tables?|collections?|indexes|indices"
    r"|buckets?|blobs?|queues?|topics?|migrations?)\b"
    # DELIBERATELY ABSENT, both learned from real issues:
    #   `container` — "capabilities: drop: [ALL]" is the RECOMMENDED pod
    #     hardening, so this parked every securityContext issue that quoted it.
    #   `secret`    — "remove the hardcoded secret" is the fix, not the damage.
    # A guard that fires on the remediation teaches people to ignore it.
    )

_FLAG = r"(?:feature[\s-]+flag|toggle)"

# How close the verb and the protected noun must be to be read as one thought.
_WINDOW = 120


def _blank(match):
    """Replace a code span with spaces of the same length.

    Same length matters: deleting it would shift every later offset and the
    proximity window would measure a distance that does not exist in the text
    the human wrote.
    """
    return " " * len(match.group(0))


def strip_code(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", _blank, text or "")
    return re.sub(r"`[^`\n]*`", _blank, text)


def match(title: str = "", body: str = ""):
    """The first destructive request found, or None.

    Returns {'kind', 'hit', 'context'} — `kind` is 'A' for a destructive verb
    near something protected and 'B' for a feature flag near a destructive
    verb, `context` is the surrounding sentence. The sentence is carried
    because "remove … tests" tells a human nothing about whether the guard is
    right, and the actual phrase tells them instantly.
    """
    text = strip_code(f"{title or ''}\n{body or ''}")

    for verb in re.finditer(_DESTRUCTIVE, text, re.IGNORECASE):
        if _NEGATION.search(text[:verb.start()]):
            continue
        for noun in re.finditer(_PROTECTED, text, re.IGNORECASE):
            if abs(verb.start() - noun.start()) >= _WINDOW:
                continue
            start = max(0, min(verb.start(), noun.start()) - 60)
            end = min(len(text), max(verb.end(), noun.end()) + 60)
            return {"kind": "A",
                    "hit": f"{verb.group(0).lower()} ... {noun.group(0).lower()}",
                    "context": " ".join(text[start:end].split())}

    for pattern in (re.compile(f"{_FLAG}[\\s\\S]{{0,200}}?{_DESTRUCTIVE}", re.IGNORECASE),
                    re.compile(f"{_DESTRUCTIVE}[\\s\\S]{{0,200}}?{_FLAG}", re.IGNORECASE)):
        found = pattern.search(text)
        if found:
            phrase = " ".join(found.group(0).split())
            return {"kind": "B", "hit": phrase[:80].lower(), "context": phrase}

    return None


def ask_note(hit: dict, mention: str, bot: str) -> str:
    """The question, in the shape both callers post and both callers detect.

    One implementation on purpose: the planner asks it first now, and if the
    solver phrased it differently the solver would not recognise the planner's
    note and would ask the same question again — which is the whole complaint
    this was written to answer.

    `mention` NAMES ONE PERSON, or is empty. Never a group, an organisation or
    a team: this note @-mentions whatever it is given, and a group mention
    notifies every member of it — this question went to forty-two people
    because the caller passed the first segment of the project path. An empty
    mention drops the "@" line's address rather than writing a bare "@", and
    the question is still asked: not knowing who to ask is not a reason to
    skip a destructive-change confirmation.
    """
    address = f"@{mention} — " if str(mention or "").strip() else ""
    lines = [f"🛑 {ASK_MARKER}", "",
             f"{address}I need clarification before writing any code.", "",
             f"The issue wording matches a destructive-change pattern "
             f"(`{hit.get('hit', '')}`)."]
    context = hit.get("context")
    if context:
        lines += ["", "The wording it matched:", "",
                  f"> …{context}…", "",
                  "If that reads like a false positive to you, say so and I'll "
                  "proceed as written."]
    lines += ["", "Before I proceed, please confirm:",
              "1. **Why** specifically should this be removed/disabled? "
              "(one concrete consequence)",
              "2. **Scope** — full removal, or only the parts causing pain?",
              "3. Any **replacement/equivalent** I should add alongside?", "",
              f"Reply mentioning `@{bot}` (or remove the **On Hold** label) "
              "and I'll proceed."]
    return "\n".join(lines)


def ask_note_id(notes, bot: str):
    """The id of the bot's ASK note, or None.

    The solver needs this, not just a yes/no, to rewind its note cursor to the
    moment the question was asked.

    Without it the adopt path swallows the answer: on the FIRST solver run the
    cursor is initialised to the NEWEST note, and when the planner asked first
    that newest note is the human's reply. The solver then sees no new
    mentions, finds On Hold still on, and parks again — while the planner,
    reading the same reply, un-parks and re-spawns it. A tick every five
    minutes, forever, doing nothing. This does not arise when the SOLVER asked,
    because the cursor was set before the reply existed.
    """
    bot = str(bot or "").lower()
    newest = None
    for note in notes or []:
        if not isinstance(note, dict) or note.get("system"):
            continue
        if str(((note.get("author") or {}).get("username") or "")).lower() != bot:
            continue
        if ASK_MARKER not in str(note.get("body") or ""):
            continue
        try:
            nid = int(note.get("id"))
        except (TypeError, ValueError):
            continue
        if newest is None or nid > newest:
            newest = nid
    return newest


def already_asked(notes, bot: str) -> bool:
    """Has the bot already put this question on the issue?

    Read from the NOTES rather than from a marker file, because the two
    callers do not share a filesystem: the planner runs in the cron pod and
    the solver in the claw-code pod. A marker written by one is invisible to
    the other, so the solver would re-ask what the planner had just asked.
    """
    bot = str(bot or "").lower()
    for note in notes or []:
        if not isinstance(note, dict) or note.get("system"):
            continue
        if str(((note.get("author") or {}).get("username") or "")).lower() != bot:
            continue
        if ASK_MARKER in str(note.get("body") or ""):
            return True
    return False

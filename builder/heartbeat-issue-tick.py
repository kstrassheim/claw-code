#!/usr/bin/env python3
"""
heartbeat-issue-tick: emit a JSON spawn plan for the issue-watcher CronJob.

Lists every open issue assigned to the bot, on every code host this
deployment has credentials for (one cross-project call each), then
queries the openclaw container's filesystem to see which repos
already have an in-flight fixer (lockdir under
~/.openclaw/projects/<repo>/.fixer.lock). At most ONE fixer per repo
runs at a time — they share the on-disk checkout, so two subprocesses
on the same repo would race.

Lock TTL: if a fixer dies without its bash `trap` firing, the lock
stays. The planner treats locks older than HEARTBEAT_TTL_SECONDS as
stale and ignores them (the next fixer will reuse the dir, and the
`mkdir` race resolves cleanly).

WHAT IS ALLOWED TO BE SPAWNED
-----------------------------
Being assigned an issue is how somebody ASKS for work; it is not an
authorisation, and it is not a schedule. Four gates stand between "the
API returned this issue" and "a solver is started on it", in this
order, because each one is cheaper than the one after it:

  1. the repository is on the owner's allowed list — project_allowlist;
     a refusal is reported with the module's own reason vocabulary
     (`allowlist-unavailable` / `allowlist-empty` / `not-permitted`) so
     the spawner can tell "could not read the list" from "not on it";
  2. the work-item STATUS is one a planner may pick up — issue_status;
     Done / Won't do / Duplicate are finished or somebody else's call;
  3. the issue is not parked `On Hold`, which is how a pending question
     is recorded. The person hands it back by removing the label or by
     replying with an @-mention of the bot — the ask offers both, and
     release_hold reads the second. Nothing else lifts it;
  4. the wording does not ask for something destructive —
     lexical_guard. That question is posted from HERE rather than from
     the solver, because asking costs one regex over text this tick
     already holds, while asking from the solver costs a clone, a
     checkout and a concurrency slot first.

What survives all four is ordered by issue_priority WITHIN the
in-flight rules, never over them: an issue already `In progress` is
finished before a fresh one is started, whatever the labels say.

The script writes to the code host for exactly ONE reason — gate 4's
question. Everything else it does is read-only.

EVERY QUESTION GOES THROUGH forge.py. This file contains no request, no
endpoint and no host-specific field name: it decides, and the forge answers.
Which forge answers is decided PER ISSUE, from where the issue was
discovered, so a deployment with two hosts works both in the same tick and a
deployment with one behaves exactly as it always did.

Env:
  GITHUB_TOKEN              — bot's credentials on GitHub
  GITLAB_URL / GITLAB_API_TOKEN
                            — bot's credentials on GitLab; unset means the
                              host is skipped, which is the normal state
  HEARTBEAT_MAX_PER_REPO    (default 1)
  HEARTBEAT_TTL_SECONDS     (default 3600)
  REVIEW_WAIT_TTL           (default 7200) — how long an awaiting-review
                            marker is believed; see list_wait_markers
"""

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# The builder modules are installed FLAT alongside this script in the image,
# and imported by bare name. Resolving from __file__ rather than hardcoding
# /usr/local/bin keeps a checkout running against its own siblings.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forge  # noqa: E402
import issue_priority  # noqa: E402
import issue_status  # noqa: E402
import approval_release
import lexical_guard  # noqa: E402
import project_allowlist  # noqa: E402
import queue_state  # noqa: E402
import story_estimate  # noqa: E402
from project_allowlist import Allowlist  # noqa: E402

MAX_PER_REPO = int(os.environ.get("HEARTBEAT_MAX_PER_REPO", "1"))
TTL_SECONDS = int(os.environ.get("HEARTBEAT_TTL_SECONDS", "3600"))
# How long the solver's "I asked for a review and am waiting" marker is
# believed. Past it the wait is ignored — see list_wait_markers.
REVIEW_WAIT_TTL = int(os.environ.get("REVIEW_WAIT_TTL", "7200"))

K8S_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_API = "https://kubernetes.default.svc"
HTTP_TIMEOUT = 15

# The label that parks an issue on a person. Matched case-insensitively and
# with any scope prefix tolerated (`Status::On Hold`, `on-hold`), because it
# is applied by hand and nobody types a label the same way twice.
ON_HOLD = "on hold"


# The code hosts this deployment has credentials for, and nothing else.
#
# Built once at import so the whole tick asks the same objects — which is also
# what makes the identity lookup a single request rather than one per issue.
# Replaceable by the tests, which drive a fake in its place and so make no
# request at all.
FORGES = forge.configured()


def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def list_all_assigned_open_issues() -> dict[str, list[dict]]:
    """Every open issue assigned to the bot, keyed by `owner/name`.

    One call per host, merged. The forge each repository came from is recorded
    as it goes, so every later question about that repository — its comments,
    its change requests — goes back to the host that answered the first one.
    """
    return FORGES.assigned_open_issues()


def forge_of(item) -> forge.Forge:
    """The host an issue (or a repository name) belongs to."""
    return FORGES.of(item)


def bot_login(f: forge.Forge) -> str:
    """The account name a host's credentials authenticate as, or "".

    Resolved rather than configured for the same reason the solver resolves
    it: sibling deployments run under different accounts, and a hardcoded
    login makes "has the bot already asked this?" answer about somebody else.

    An identity that cannot be read is "" rather than a failure: the tick has
    a dozen other things to do, and every caller of this treats an unknown
    login as "nobody has spoken yet", which is the cautious direction.
    """
    try:
        return f.bot_identity()
    except Exception:  # noqa: BLE001
        return ""


def read_allowlist(namespace: str, pod: str) -> Allowlist:
    """The owner's allowed-projects list, read out of the openclaw pod.

    An exec that fails produces an UNAVAILABLE list rather than an empty one.
    Both permit nothing — see project_allowlist.py on why that direction is
    the safe one — but only one of them is a fault somebody has to fix, and
    the plan says which.
    """
    try:
        rc, out, err = kubectl_exec_capture(
            namespace, pod, "sh", "-c", project_allowlist.pod_read_snippet())
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"read_allowlist: {type(e).__name__}: {e}\n")
        return Allowlist.denied()
    if rc != 0:
        sys.stderr.write(f"read_allowlist: exec rc={rc} stderr={err}\n")
        return Allowlist.denied()
    lines = [l for l in out.splitlines()
             if l.strip() != project_allowlist.ALLOWED_MARKER]
    return project_allowlist.from_section(lines)


def label_names(issue: dict) -> list[str]:
    """Every label name on an issue.

    A list of names is what the forge hands back, whatever the host called
    them underneath. Kept as a function rather than inlined because it is
    read in four places and one of them is the plan the spawner consumes.
    """
    return [str(n) for n in (issue.get("labels") or []) if str(n).strip()]


def _fold(name: str) -> str:
    """A label name reduced to its comparable core: no scope, no punctuation.

    `On Hold`, `on-hold`, `Status::On Hold` and `ON_HOLD` are one instruction
    typed by four people, and a park that only recognises one spelling is a
    park that silently does not happen.
    """
    text = str(name or "").strip().lower()
    if "::" in text:
        text = text.rsplit("::", 1)[-1]
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def is_on_hold(issue: dict) -> bool:
    """True while the issue is parked waiting on a person.

    The release stays a deliberate human act — an issue cannot drift back
    into the queue because a run decided the question had been answered well
    enough. What counts as the act is the one the ask names: removing the
    label, or replying with an @-mention of the bot. `release_hold` reads
    the second and takes the label off; nothing else lifts it.
    """
    return any(_fold(name) == ON_HOLD for name in label_names(issue))


def status_of_issue(issue: dict) -> str:
    """The work-item status of an issue, in issue_status' vocabulary.

    A CLOSED issue says how it ended, in intent — the work shipped, or it was
    called off — and that beats any label still stuck to it. Which field or
    label the host recorded that in is the forge's business; the answer here
    is the same either way.
    """
    return issue_status.status_of_item(
        label_names(issue),
        state=issue.get("state") or "open",
        closed_as=issue.get("closedAs"),
    )


def answer_after(notes, bot: str, anchor):
    """The id of the first note that ANSWERS the bot after `anchor`, or None.

    An answer is a note from somebody other than the bot that @-mentions it —
    the act the ask asks for: "Reply mentioning `@bot` ... and I'll proceed".
    Deliberately stricter than `human_has_answered`, which only asks whether a
    person spoke last: that ranks a marker park, and being wrong there costs
    one spawn that exits in seconds. Both gates that use this one hold back
    destructive-sounding work, so bystander chatter is not a go-ahead.

    One definition, two callers, because they must not be able to disagree:
    release_hold takes the label off when this returns an id, and
    ask_before_spawning has to reach the same verdict about the same reply or
    the issue is released into a gate that still refuses it.
    """
    bot = str(bot or "").lower()
    if not bot or anchor is None:
        return None
    mention = f"@{bot}"
    for note in notes or []:
        if not isinstance(note, dict) or note.get("system"):
            continue
        try:
            nid = int(note.get("id"))
        except (TypeError, ValueError):
            continue
        if nid <= anchor:
            continue
        if str(((note.get("author") or {}).get("username") or "")).lower() == bot:
            continue
        if mention in str(note.get("body") or "").lower():
            return nid
    return None


def ask_before_spawning(f: forge.Forge, repo: str, issue: dict,
                        bot: str) -> bool:
    """True ⟺ this issue must NOT be spawned: it asks for something
    destructive and a human has to confirm first.

    WHY HERE AND NOT ONLY IN THE SOLVER
    The solver has always had this check, but it runs it AFTER being spawned —
    after the clone, the checkout and the concurrency slot. Every
    destructive-sounding issue therefore cost a full solver start to reach a
    question that needs no model at all. Asking here costs one regex over text
    this tick already has.

    The solver keeps its copy: an issue can be reworded after it was planned,
    and the guard must not depend on which process saw it first. Both use
    lexical_guard, so the solver recognises THIS note and adopts the question
    rather than asking it twice.

    Returns True on a write failure as well. An issue that should have been
    questioned and could not be is an issue to leave alone, not one to hand to
    an agent — the safe direction is the one that does no work.
    """
    hit = lexical_guard.match(issue.get("title", ""), issue.get("body", ""))
    if not hit:
        return False

    number = issue["number"]
    try:
        comments = f.comments(repo, number)
    except Exception:  # noqa: BLE001
        # Could not find out whether it was already asked. Asking again would
        # spam the issue; spawning would skip the gate. Do neither.
        sys.stderr.write(f"  {repo}#{number}: could not read comments — "
                         "not spawning\n")
        return True

    if lexical_guard.already_asked(comments, bot):
        # The question is on the record. What ends it is the reply the ask
        # asked for, and nothing else.
        #
        # This used to read "released by a human taking the On Hold label
        # off, which is checked before this" — and that was never true. The
        # label gate excludes an issue that still HAS the label; an issue
        # reaching here has already lost it, and this returned True anyway.
        # So a guard-questioned issue could not be spawned by any means: not
        # by answering, not by taking the label off by hand. Observed on an
        # issue answered "its ok continue" that then sat for four days.
        answer = answer_after(comments, bot,
                              lexical_guard.ask_note_id(comments, bot))
        if answer is not None:
            sys.stderr.write(f"  {repo}#{number}: question answered in note "
                             f"{answer} — spawning\n")
            return False
        return True

    # The repo OWNER, not the issue author: the bot may open issues itself
    # later, and pinging the author would then ping the bot.
    mention = repo.split("/", 1)[0]
    body = lexical_guard.ask_note(hit, mention, bot)
    if not f.post_comment(repo, number, body):
        sys.stderr.write(f"  {repo}#{number}: could not post the "
                         "confirmation question — not spawning\n")
        return True

    # On Hold is what keeps the planner from re-reading this issue every five
    # minutes, and what the human removes to say "go ahead".
    f.add_labels(repo, number, ["On Hold"])
    sys.stderr.write(f"  asked before spawning {repo}#{number}: "
                     f"{hit.get('hit', '')}\n")
    return True


def on_hold_label_name(issue: dict) -> str:
    """The park label as THIS issue spells it, or "".

    `remove_label` deletes by exact name, and the park is recognised through
    `_fold` — so `Status::On Hold` and `on-hold` are both a park, and neither
    can be removed by passing the literal "On Hold". The spelling has to come
    back off the issue it is being removed from.
    """
    for name in label_names(issue):
        if _fold(name) == ON_HOLD:
            return name
    return ""


def _release_pr_park(f: forge.Forge, repo: str, number: int, name: str,
                     kind: str, ask: dict, notes, bot: str):
    """Lift a pull-request park, or None to fall through to the reply rule.

    None rather than False is the point: an unreadable pull request is not a
    verdict, and the @-mention path below still has to get its turn. Only a
    definite answer — signed off, rejected, or landed — returns here.
    """
    pr = ask.get("pr")
    if not pr:
        return None

    def drop(why: str):
        if f.remove_label(repo, number, name):
            sys.stderr.write(f"  released {repo}#{number}: '{name}' removed "
                             f"— {why}\n")
            return True
        sys.stderr.write(f"  {repo}#{number}: {why}, but could not remove "
                         f"'{name}' — staying parked\n")
        return False

    if kind == "blocked":
        # Waiting for a person to press merge. What ends it is the pull
        # request no longer being open — usually because they did.
        try:
            state = str((f.change_request(repo, pr) or {}).get("state") or "")
        except Exception:  # noqa: BLE001
            return None
        if state and state.lower() != "open":
            return drop(f"PR #{pr} is {state.lower()}")
        return None

    try:
        verdicts = f.review_verdicts(repo, pr)
    except Exception:  # noqa: BLE001
        return None

    sha = ask.get("sha") or ""
    got = approval_release.signed_off(verdicts=verdicts, comments=notes,
                                      bot=bot, sha=sha, anchor_id=ask["id"])
    if got:
        return drop(f"@{got['who']} {got['how']} (#{pr})")

    # A rejection releases the park too, and this is not a special case: the
    # reviewer who asks for changes has handed the issue BACK. Leaving it
    # parked would leave what they typed unread — the failure the park was
    # meant to prevent, pointed the other way.
    rejected = approval_release.changes_requested(verdicts=verdicts, bot=bot,
                                                  sha=sha)
    if rejected:
        return drop(f"@{rejected['who']} {rejected['how']} on #{pr} "
                    "— back to the solver")
    return None


def release_hold(f: forge.Forge, repo: str, issue: dict, bot: str) -> bool:
    """Take `On Hold` off when the person has answered. True if released.

    WHY THE BOT MAY LIFT ITS OWN PARK.
    The ask promises it: "Reply mentioning `@bot` (or remove the On Hold
    label) and I'll proceed" — see lexical_guard.ask_body. Only the
    parenthetical was ever wired, so the reply the message asks for did
    nothing: the label gate below drops the issue before any comment is read,
    and the answer sits unread forever. Observed on eight issues answered
    within ten minutes of each other, none of which moved.

    WHAT COUNTS AS AN ANSWER.
    A reply that @-mentions the bot, posted after the bot asked. Not merely
    "a person spoke last" — that is `human_has_answered`, which ranks a
    MARKER park and is deliberately looser because the cost of being wrong
    there is one spawn that exits in seconds. This park guards
    destructive-sounding work, so the bar is the one the ask names, and
    bystander chatter on the issue is not a go-ahead.

    Fails toward STAYING PARKED. Every unreadable case here ends in False:
    a park that outlives its answer costs a reply; a park lifted on a
    question nobody answered costs whatever the issue asked for.
    """
    number = issue.get("number")
    name = on_hold_label_name(issue)
    if not number or not name:
        return False
    bot = str(bot or "").lower()
    if not bot:
        return False
    try:
        notes = f.comments(repo, number)
    except Exception:  # noqa: BLE001
        return False

    # (id, author, body) for every note that has a usable id. Built once so
    # the anchor and the answer are read from the same rows, in one pass.
    rows = []
    for note in notes:
        if not isinstance(note, dict) or note.get("system"):
            continue
        try:
            nid = int(note.get("id"))
        except (TypeError, ValueError):
            continue
        rows.append((nid,
                     str(((note.get("author") or {}).get("username")
                          or "")).lower(),
                     str(note.get("body") or "")))

    # Where the wait started. The ASK note when the guard asked; otherwise the
    # bot's newest note, which is where a solver-side park (fixer-runner's
    # `park`) put the question. Without an anchor the bot never spoke, so
    # this is not its park to lift.
    anchor = lexical_guard.ask_note_id(notes, bot)
    if anchor is None:
        mine = [nid for nid, author, _ in rows if author == bot]
        anchor = max(mine) if mine else None
    if anchor is None:
        return False

    # WHICH ACT ENDS THIS WAIT DEPENDS ON WHY IT STARTED.
    #
    # `On Hold` reads the same to a person either way — "waiting on you" — but
    # the bot is not waiting for the same thing. It asked a QUESTION (the
    # lexical guard, a CI-red escalation), and only an answer addressed to it
    # ends that; or it asked for a SIGN-OFF, and what ends that is approving
    # the pull request. Demanding an @-mention for the second would mean the
    # reviewer approves, nothing happens, and the only symptom is silence.
    #
    # Both branches are decided by `approval_release`, which the solver's
    # approval gate imports too: if these two ever disagreed, the issue would
    # be released into a gate that re-asks and re-parks it every tick.
    kind, ask = approval_release.newest_park_ask(notes, bot)
    if kind and ask["id"] >= anchor:
        released = _release_pr_park(f, repo, number, name, kind, ask,
                                    notes, bot)
        if released is not None:
            return released

    mention = f"@{bot}"
    for nid, author, body in rows:
        if nid <= anchor or author == bot:
            continue
        if mention not in body.lower():
            continue
        if f.remove_label(repo, number, name):
            sys.stderr.write(f"  released {repo}#{number}: "
                             f"'{name}' removed — answered in note {nid}\n")
            return True
        sys.stderr.write(f"  {repo}#{number}: answered but could not remove "
                         f"'{name}' — staying parked\n")
        return False
    return False


def human_has_answered(repo: str, issue: dict, bot: str) -> bool:
    """Has a person replied since the bot last asked?

    WHY THE PLANNER ASKS THIS AND NOT THE SOLVER.

    An issue parked awaiting a human ranks LAST so the bot moves on to work it
    can influence. The obvious design is to let the SOLVER clear the park when
    it next runs and sees a reply — which is what the comment here used to
    claim happened.

    It cannot. Ranked last, with any backlog at all, the issue is never
    spawned; never spawned, the marker is never cleared; never cleared, it
    stays ranked last. The park becomes permanent the moment there is other
    work, and answering the question does not release it. Observed on an
    issue that sat seven hours after the human replied, with the reply sitting
    unread on the issue the whole time.

    So the planner decides it, from the host, without needing the solver to
    run at all. Both places a person can answer are checked, because the
    handoff asks them to act in either: a comment on the ISSUE, or a comment
    or a review on the change request. Either resumes the issue.

    Fails toward RESUMING. Anything we cannot read here means the issue is
    worked normally rather than parked, and the cost of being wrong that way
    is one spawn that exits in seconds — against a park that never lifts.
    """
    number = issue.get("number")
    if not number:
        return True

    def newest_is_the_bot(notes) -> bool:
        rows = [c for c in (notes or []) if isinstance(c, dict)]
        if not rows:
            return False          # nothing said at all — nobody is waiting
        author = ((rows[-1].get("author") or {}).get("username") or "").lower()
        return author == (bot or "").lower()

    f = forge_of(issue if issue.get("forge") else repo)
    try:
        if not newest_is_the_bot(f.comments(repo, number)):
            return True           # a person spoke last

        # They may have answered on the change request instead — that is where
        # a handoff asks them to act.
        for cr in f.open_change_requests_for_issue(repo, number):
            if not newest_is_the_bot(f.change_request_comments(repo, cr)):
                return True
            for verdict in f.review_verdicts(repo, cr):
                who = str(verdict.get("author") or "").lower()
                if who and who != (bot or "").lower():
                    return True
    except Exception:  # noqa: BLE001
        return True               # cannot tell → resume
    return False


def k8s_find_openclaw_pod(namespace: str) -> str:
    token = _read(f"{K8S_SA_DIR}/token")
    ctx = ssl.create_default_context(cafile=f"{K8S_SA_DIR}/ca.crt")
    url = f"{K8S_API}/api/v1/namespaces/{namespace}/pods?labelSelector=app%3Dopenclaw"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
        body = json.loads(r.read())
    for item in body.get("items", []):
        if item.get("status", {}).get("phase") == "Running":
            return item["metadata"]["name"]
    raise RuntimeError("no Running openclaw pod found")


def kubectl_exec_capture(namespace: str, pod: str, *cmd: str, timeout: int = 15) -> tuple[int, str, str]:
    """Run a command inside the openclaw pod, capture stdout/stderr."""
    full = ["kubectl", "-n", namespace, "exec", pod, "-c", "openclaw", "--", *cmd]
    proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def list_locked_repos(namespace: str, pod: str) -> set[str]:
    """Return set of repo full_names with a LIVE in-flight fixer. A lock dir
    under ~/.openclaw/.fixer-locks/<owner>__<name>/ counts as held only if it
    is newer than TTL_SECONDS AND its owner PID is still alive in this pod.
    An orphaned lock (owner died without its EXIT trap firing — e.g. the pod
    was redeployed mid-fix) is NOT counted, so the repo is eligible again
    immediately instead of waiting out the TTL. The spawned fixer-runner makes
    the same stale check before claiming, so this only governs scheduling."""
    # `find` is faster than a recursive ls; the lock-set is small
    # (≤ one dir per repo the bot is a collaborator on). Lock
    # dirs are siblings of the project tree (NOT inside it — a
    # `.fixer.lock` inside the project dir broke `git clone`). This script
    # runs INSIDE the openclaw pod, so `kill -0` sees the fixer PIDs.
    script = (
        "set -eu; root=$HOME/.openclaw/.fixer-locks; "
        "[ -d $root ] || exit 0; "
        f"now=$(date +%s); ttl={TTL_SECONDS}; "
        "for lock in $(find $root -maxdepth 1 -mindepth 1 -type d 2>/dev/null); do "
        "  age=$(( now - $(stat -c %Y \"$lock\") )); "
        "  [ $age -lt $ttl ] || continue; "  # older than TTL → stale, not held
        "  pid=$(awk 'NR==1{print $1}' \"$lock/owner\" 2>/dev/null || true); "
        # owner PID recorded but no longer alive → orphaned lock, not held
        "  if [ -n \"$pid\" ] && ! kill -0 \"$pid\" 2>/dev/null; then continue; fi; "
        # Lock dir name is owner__name → emit owner/name
        "  basename \"$lock\" | sed 's|__|/|'; "
        "done"
    )
    rc, out, err = kubectl_exec_capture(namespace, pod, "bash", "-c", script)
    if rc != 0:
        sys.stderr.write(f"list_locked_repos: exec rc={rc} stderr={err}\n")
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def list_wait_markers(namespace: str, pod: str) -> tuple[set, set]:
    """What each repo's solver is currently WAITING on, read off the PVC.

    Two marker kinds live in ~/.openclaw/issue-markers/, both written by the
    solver and both named <owner>__<repo>-<number>.<kind>:

      .awaiting-review  the solver asked the autonomous reviewer for a verdict
                        on its own pull request and is waiting for it;
      .awaiting-human   the solver asked a PERSON — a sign-off, or an
                        escalation after a retry budget ran out.

    They rank in opposite directions, which is the whole point of reading
    them. A review the bot is waiting on is still the BOT'S work: it should
    finish that issue before starting another, so it ranks FIRST. A wait on a
    person is out of the bot's hands and can last days, so it ranks LAST and
    the bot spends its one slot per repo on something it can actually move.

    THE AWAITING-REVIEW MARKERS EXPIRE, and that is a safety net rather than a
    detail. Ranking an issue first because the solver is waiting is only
    correct while somebody is actually going to answer. A reviewer that
    crashed, was suspended, or never posted its verdict leaves a marker that
    would otherwise pin the repo's single slot on an issue that spends zero
    model calls per tick, forever — the deadlock this exists to bound. Past
    REVIEW_WAIT_TTL the marker is ignored here, the issue re-ranks as ordinary
    work, and the solver re-checks the pull request and re-requests the
    review.

    One exec for both kinds: a marker read is worth a round trip, not two.
    Anything that stops us reading them returns empty sets — the ordering is
    an optimisation, and a tick that cannot read the markers should still
    plan.
    """
    script = (
        "root=$HOME/.openclaw/issue-markers; "
        '[ -d "$root" ] || exit 0; '
        f"now=$(date +%s); ttl={REVIEW_WAIT_TTL}; "
        'for f in "$root"/*.awaiting-review; do '
        '  [ -e "$f" ] || continue; '
        '  age=$(( now - $(stat -c %Y "$f" 2>/dev/null || echo 0) )); '
        "  [ $age -lt $ttl ] || continue; "
        '  echo "review $(basename "$f")"; '
        "done; "
        'for f in "$root"/*.awaiting-human; do '
        '  [ -e "$f" ] || continue; '
        '  echo "human $(basename "$f")"; '
        "done"
    )
    try:
        rc, out, err = kubectl_exec_capture(namespace, pod, "bash", "-c", script)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"list_wait_markers: {type(e).__name__}: {e}\n")
        return set(), set()
    if rc != 0:
        sys.stderr.write(f"list_wait_markers: exec rc={rc} stderr={err}\n")
        return set(), set()
    review: set = set()
    human: set = set()
    for line in out.splitlines():
        m = re.match(r"^(review|human)\s+(.*)-(\d+)\.awaiting-(?:review|human)$",
                     line.strip())
        if not m:
            continue
        # The marker spells owner/name with a double underscore, because a
        # slash cannot be a filename.
        key = (m.group(2).replace("__", "/"), int(m.group(3)))
        (review if m.group(1) == "review" else human).add(key)
    return review, human


def main() -> int:
    if not FORGES:
        json.dump({"error": "no code host is configured (set GITHUB_TOKEN, "
                            "or GITLAB_URL and GITLAB_API_TOKEN)"}, sys.stdout)
        return 1
    try:
        namespace = _read(f"{K8S_SA_DIR}/namespace")
    except FileNotFoundError:
        json.dump({"error": "not running in a pod (no service-account dir)"}, sys.stdout)
        return 1

    started = time.time()
    try:
        issues_by_repo = list_all_assigned_open_issues()
    except forge.ForgeError as e:
        json.dump({"error": f"list assigned issues: {e}"}, sys.stdout)
        return 2

    try:
        openclaw_pod = k8s_find_openclaw_pod(namespace)
    except Exception as e:
        json.dump({"error": f"find openclaw pod: {e}"}, sys.stdout)
        return 3

    allowed = read_allowlist(namespace, openclaw_pod)
    locked = list_locked_repos(namespace, openclaw_pod)
    # What each solver is waiting on. Read once for every repo in the tick,
    # because it is one exec against one directory either way.
    awaiting_review, awaiting_human = list_wait_markers(namespace, openclaw_pod)

    plan: dict = {
        "generatedAt": int(started),
        "namespace": namespace,
        "openclawPod": openclaw_pod,
        "ttlSeconds": TTL_SECONDS,
        "maxPerRepo": MAX_PER_REPO,
        # Surfaced so a tick that spawned nothing says why: "the owner has
        # permitted 0 repositories" and "I could not read the list" are very
        # different situations that look identical in the repos array.
        "allowedProjects": len(allowed) if allowed.available else None,
        "allowlistAvailable": allowed.available,
        "repos": [],
    }

    for repo, all_issues in sorted(issues_by_repo.items()):
        # Which host this repository lives on, decided from where its issues
        # were discovered rather than from a deployment-wide setting. Read
        # once per repository: every question below asks the same one.
        f = forge_of(all_issues[0] if all_issues else repo)

        # Permission first: before the lock lookup, before any per-issue call,
        # before the destructive-wording question is even considered. Someone
        # assigning the bot an issue is a request; this list is the answer.
        if not allowed.allows(repo):
            plan["repos"].append({
                "repo": repo,
                "locked": False,
                "totalAssigned": len(all_issues),
                "openAssignedCount": 0,
                "toSpawn": [],
                "deferredDueToLimit": 0,
                "reason": allowed.deny_reason(repo),
            })
            continue

        # Status gate: keep only what a planner may pick up. `Done` is
        # delivered, `Won't do` and `Duplicate` are a human's terminal call —
        # all three are closed, and re-planning a closed issue is how a bot
        # re-opens work somebody deliberately ended.
        workable = [i for i in all_issues
                    if issue_status.is_workable(status_of_issue(i))]
        dropped_by_status = len(all_issues) - len(workable)

        # Who this tick speaks as on THIS host. Resolved here rather than at
        # the top of the loop so a repository the owner refused costs no
        # request at all — the allowlist gate is first for that reason, and an
        # identity lookup would have quietly undone it. It now has to be known
        # BEFORE the On Hold gate, which reads who said what.
        bot = bot_login(f)

        # On Hold: a question is pending, so the issue is not the bot's to
        # move — until the person answers. `release_hold` takes the label off
        # when they have, which is what the ask told them to do; anything it
        # cannot read stays parked. Applied after the status gate so the two
        # are reported apart — "closed" and "waiting on a person" are
        # different situations and a tick that spawned nothing has to say
        # which one it was.
        parked = []
        for i in workable:
            if not is_on_hold(i):
                continue
            if release_hold(f, repo, i, bot):
                continue          # answered: label gone, stays workable
            parked.append(i)
        if parked:
            held = {id(i) for i in parked}
            workable = [i for i in workable if id(i) not in held]

        # Ask about destructive-sounding work BEFORE spawning anything.
        questioned = [i for i in workable
                      if ask_before_spawning(f, repo, i, bot)]
        if questioned:
            asked = {id(i) for i in questioned}
            workable = [i for i in workable if id(i) not in asked]

        issues = workable
        is_locked = repo in locked
        # MAX_PER_REPO is 1 by design — checkouts can't be shared.
        # If the lock is held, we skip all issues for this repo until
        # the next tick.
        if is_locked or not issues:
            to_spawn = []
            deferred = len(issues) if is_locked else 0
        else:
            # With MAX_PER_REPO=1 the planner services ONE issue per tick, so
            # this sort decides which. In order:
            #
            #   rank      what the bot can actually finish, first. An issue
            #             already `In progress` is finished before a fresh one
            #             is started — the in-flight rule, which a priority
            #             label must not be able to undo, because the bot
            #             converging on one issue at a time is what keeps it
            #             from leaving a trail of half-done branches. Two
            #             kinds of WAIT bracket that rule, in opposite
            #             directions; see _rank.
            #   priority  issue_priority orders what is left, most urgent
            #             first, defaulting to Medium.
            #   number    ascending, so equal work is FIFO / oldest first.
            def _rank(i: dict) -> int:
                # A pull request parked on the bot's OWN reviewer ranks FIRST,
                # not last. When the bot is its own reviewer the work is still
                # the bot's, so it sees that issue through to the merge before
                # starting another. With MAX_PER_REPO=1 this deliberately
                # holds the repo's single slot: each tick re-spawns THIS
                # issue, the solver finds no verdict yet and exits in seconds,
                # and the moment the verdict lands the solver acts on it.
                # That is only safe because the marker EXPIRES — a reviewer
                # that never delivers would otherwise pin the slot forever.
                # See list_wait_markers and REVIEW_WAIT_TTL.
                if (repo, i["number"]) in awaiting_review:
                    return 0
                # A wait on a PERSON is the opposite case: out of the bot's
                # hands, possibly for days. Those rank LAST so the bot moves
                # on to work it can influence. The solver drops the marker the
                # moment the human answers, which puts the issue straight back
                # into the ordinary order.
                # ...but ONLY while the person is still silent. Checking the
                # marker alone made the park permanent: ranked last, the issue
                # is never spawned, so the solver never runs to clear it. The
                # answer has to lift the rank without the solver's help.
                if (repo, i["number"]) in awaiting_human \
                        and not human_has_answered(repo, i, bot):
                    return 3
                return 1 if status_of_issue(i) == issue_status.IN_PROGRESS else 2

            ordered = sorted(
                issues,
                key=lambda i: (_rank(i),
                               issue_priority.priority_of(i.get("labels")),
                               i["number"]),
            )
            to_spawn = [
                {
                    "issueNumber": i["number"],
                    "title": i["title"],
                    "url": i["url"],
                    "labels": label_names(i),
                    "status": status_of_issue(i),
                    "priority": issue_priority.label_for(
                        issue_priority.priority_of(i.get("labels"))),
                    # The size, and whether it was judged or assumed. Both
                    # travel to the spawner: the solver picks its model from
                    # the points, and a DEFAULTED 8 must never be reported as
                    # an estimate somebody made.
                    "storyPoints": story_estimate.effective_points(i)[0],
                    "pointsDefaulted": story_estimate.effective_points(i)[1],
                    # Size first, implement next tick. The model a run gets
                    # depends on the size, so sizing inside the run that has
                    # already chosen a model would be circular.
                    "needsEstimate": story_estimate.needs_estimate(
                        i.get("labels")),
                }
                for i in ordered[:MAX_PER_REPO]
            ]
            deferred = max(0, len(issues) - MAX_PER_REPO)

        plan["repos"].append(
            {
                "repo": repo,
                "locked": is_locked,
                "totalAssigned": len(all_issues),
                "openAssignedCount": len(issues),
                "notWorkable": dropped_by_status,
                "onHold": len(parked),
                "awaitingConfirmation": len(questioned),
                "toSpawn": to_spawn,
                "deferredDueToLimit": deferred,
            }
        )

    # Order the repositories by the most urgent thing each of them offers, so
    # the spawner walks the plan most-urgent-first ACROSS repositories. Not
    # cosmetic: the three subsystems share one model-concurrency gate, so when
    # slots are scarce, spawn order decides who gets one. Repos with nothing
    # to spawn sort last and keep their alphabetical order.
    def _repo_key(r):
        spawn = r.get("toSpawn") or []
        if not spawn:
            return (1, issue_priority.DEFAULT_LEVEL, r["repo"])
        best = min(issue_priority.LEVELS.get(
            issue_priority._normalise(e.get("priority", "")),
            issue_priority.DEFAULT_LEVEL) for e in spawn)
        return (0, best, r["repo"])

    plan["repos"].sort(key=_repo_key)

    # Publish how much work is still queued so the deployment tester can hold
    # off until it is gone ("first solve and merge, then test"). This is the
    # count AFTER the allowlist, the status gate and the On Hold park, which
    # is exactly why it is published from here rather than re-derived by the
    # tester: an issue parked on a human is not pending work, and counting it
    # as such would disable testing indefinitely.
    #
    # Best-effort by design. A failed publish leaves a stale marker, and the
    # tester reads a stale marker as unknown and runs — see queue_state.py.
    pending_issues = sum(r.get("openAssignedCount", 0) for r in plan["repos"])
    plan["pendingIssues"] = pending_issues
    try:
        kubectl_exec_capture(
            namespace, openclaw_pod, "sh", "-c",
            queue_state.pod_write_snippet("solver", pending_issues))
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"publish queue depth: {type(e).__name__}: {e}\n")

    plan["elapsedSeconds"] = round(time.time() - started, 2)
    json.dump(plan, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

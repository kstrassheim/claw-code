"""Document shapes for the planning store.

Pure functions: no I/O, no database, no clock of their own. Everything that
touches the network lives in planning_store.py, so these can be tested offline
and the shapes can be argued about before any store exists.

THE KEY SCHEME
--------------
One MongoDB collection holding five `type`s, grouped by a `pk` field:

    pk = story#<host>#<owner/repo>#<issue-number>
         type=story   the story itself
         type=work    append-only, one per runner invocation

    pk = deploy#<host>#<owner/repo>#<sha12>
         type=deploy  one tested commit of the default branch
         type=work    the tester run for that commit

    pk = sprint#<n>              type=sprint
    pk = worker#<host>#<id>      type=worker

`pk` is not a database requirement — MongoDB needs no partition key — it is a
GROUPING key, and it is what makes "everything about this story" a single
indexed query over one field instead of an $or across four shapes. A story and
all of its work events share one pk for exactly that reason.

The host is part of every key on purpose. A repository is addressed as
`owner/repo` on github.com and on a self-hosted GitHub Enterprise instance
alike, so without the host segment `acme/web#12` on two hosts is one key, and
the day a second host is configured every report silently merges them.

WHY WORK EVENTS ARE APPEND-ONLY
-------------------------------
The solver and the reviewer write concurrently, and a runner flushes its call
count repeatedly during a run. If they all advanced one shared story document
we would need optimistic-concurrency retries on every flush. Writing a new
event never collides, and it also makes the local spool trivial: a spool line
is a whole document, not a patch to reconcile.

DOCUMENT IDS ARE DETERMINISTIC
------------------------------
Every `id` here is derived from what the document IS — a story from its key, a
work event from runId+role — never from a clock or a counter. The store writes
that id into MongoDB's `_id` and upserts, so flushing the same spool line twice
replaces the document instead of duplicating it. That single property is what
lets the spool be a plain append-only file with no two-phase commit.
"""

from __future__ import annotations

import re

STORY = "story"
WORK = "work"
SPRINT = "sprint"
WORKER = "worker"
# The deployment tester's unit of work, which is NOT a story.
#
# The tester keys on (repo, HEAD sha of the default branch) and keeps a
# `<repo>.last-head` marker. It knows nothing about issues or pull requests —
# it tests the RANGE priorHead..headSha and turns findings into new issues. So
# its work events have no story to live under, and inventing one would put a
# document in the store describing work on an issue that does not exist.
#
# It gets its own key per tested commit instead, and records which stories that
# commit contained. That is the honest shape: a deploy covers many stories, and
# a story can be covered by several deploys (a re-test after a fix), so the
# relationship is many-to-many and neither side owns the other.
DEPLOY = "deploy"

# One host today. The KEYS stay host-aware anyway — see the module docstring:
# adding a second host later must not silently merge two repositories that
# happen to share an `owner/repo` path.
HOSTS = ("github",)

# Roles that produce work events. The tester is here even though it does not
# work a story: it opens them, and its cost is part of what a sprint spent.
ROLES = ("solver", "reviewer", "tester", "planner")

# Where a story came from. `tester` is the one that matters for reporting:
# it is how "how much of this sprint was absorbing what the tester found"
# becomes answerable instead of guessed.
ORIGINS = ("human", "tester", "solver", "reviewer", "planner")

# HOW a story got into the sprint it is in. Three cases, not two:
#
#   committed  in the plan when the sprint started
#   added      arrived after the sprint had started — scope growth, which is
#              mostly what the deployment tester opens
#   carried    unfinished in the previous sprint and moved forward
#
# Recorded explicitly rather than derived from enteredSprintAt vs the sprint's
# start. Deriving it would make every report read the sprint document too, and
# — worse — the answer would change retroactively if anyone edited the sprint
# start. This is a fact about the moment of entry and should be frozen there.
#
# `carried` matters on its own: a carried story is not a planning miss in the
# sprint that inherits it, and counting it as committed scope there would make
# the new sprint look over-committed while hiding the previous one's shortfall.
# A fourth value exists because the first three are not always knowable:
# stories that predate this feature, or an entry with no usable timestamp,
# have no honest answer. `unknown` says so instead of guessing "committed",
# which would quietly inflate every commitment figure it appears in.
SCOPES = ("committed", "added", "carried", "unknown")

_SAFE = re.compile(r"[^A-Za-z0-9._/#-]")


def _clean(value: str) -> str:
    """Keys are compared, indexed and printed. Keep them boring."""
    return _SAFE.sub("-", str(value or "").strip())


def story_pk(host: str, repo: str, number) -> str:
    """`story#<host>#<owner/repo>#<issue-number>`.

    Callers must build keys through here rather than by formatting a string,
    so a repository name containing anything `_clean` rewrites is normalised
    identically on the write path and on every query that goes looking for it.
    """
    return f"{STORY}#{_clean(host)}#{_clean(repo)}#{_clean(number)}"


def sprint_pk(number) -> str:
    return f"{SPRINT}#{_clean(number)}"


def worker_pk(host: str, user_id) -> str:
    return f"{WORKER}#{_clean(host)}#{_clean(user_id)}"


def deploy_pk(host: str, repo: str, sha) -> str:
    """One key per tested commit of a repository's default branch."""
    return f"{DEPLOY}#{_clean(host)}#{_clean(repo)}#{_clean(str(sha)[:12])}"


def story_doc(
    *,
    host: str,
    repo: str,
    number,
    title: str,
    url: str = "",
    host_url: str = "",
    issue_type: str = "issue",
    origin: str = "human",
    origin_ref: str = "",
    category: str = "",
    story_points=None,
    points_defaulted: bool = False,
    estimator: str = "",
    estimated_at: str = "",
    sprint_id=None,
    entered_sprint_at: str = "",
    labels=None,
    priority: str = "",
    now: str = "",
) -> dict:
    """The story document. Updated as the story progresses.

    `points_defaulted` travels with the number on purpose: a defaulted 8 and a
    judged 8 are not the same fact, and a sprint whose added scope is all
    defaults is not the same sprint as one that was estimated. From the number
    alone, six weeks later, nobody can tell them apart.
    """
    pk = story_pk(host, repo, number)
    return {
        "id": pk,
        "pk": pk,
        "type": STORY,
        "host": host,
        "hostUrl": host_url,
        "repo": repo,
        "number": number,
        "issueType": issue_type,
        "title": title,
        "url": url,
        "category": category,
        "labels": list(labels or []),
        "priority": priority,
        "origin": origin,
        "originRef": origin_ref,
        "storyPoints": story_points,
        "pointsDefaulted": bool(points_defaulted),
        "estimator": estimator,
        "estimatedAt": estimated_at,
        "sprintId": sprint_id,
        "enteredSprintAt": entered_sprint_at,
        # How it got into that sprint — see SCOPES. Frozen at entry.
        "sprintScope": "unknown",
        # One entry per sprint this story has been in, so a story that spans
        # sprints keeps its own account of it. The sprint documents hold the
        # authoritative lists; this is what makes the STORY answerable on its
        # own ("was this ever carried?") without reading every sprint.
        "sprintHistory": [],
        # Filled in as the story moves. Absent is meaningful: it has not
        # happened yet, which is different from happening at an unknown time.
        "startedAt": None,
        "prOpenedAt": None,
        "reviewRequestedAt": None,
        "mergedAt": None,
        "prUrl": None,
        "summary": None,
        "updatedAt": now,
    }


def work_doc(
    *,
    host: str,
    repo: str,
    number,
    run_id: str,
    role: str,
    worker: str = "",
    model: str = "",
    provider: str = "",
    llm_calls: int = 0,
    seconds_on_429: int = 0,
    started_at: str = "",
    ended_at: str = "",
    outcome: str = "",
    sprint_id=None,
    expires_at: str = "",
    now: str = "",
) -> dict:
    """One runner invocation. Append-only — never updated in place.

    `model` and `provider` sit here rather than on the worker because they
    vary per run: the model can be switched between two runs of the same
    story. Recording them per run is what makes "did the cheaper model really
    perform comparably on small stories" a query instead of an opinion.

    `expires_at` is an ISO timestamp for a TTL index to act on. Work events are
    the bulk of the volume and the least valuable once their sprint is closed;
    everything else here is kept indefinitely and therefore carries no such
    field at all, which is precisely how a MongoDB TTL index skips a document.
    """
    doc = {
        # The run id makes this idempotent: a retried flush replaces its own
        # document rather than appending a duplicate of the same run. It is
        # also why planning-record can be called on a timer during a long run.
        "id": f"{WORK}#{_clean(run_id)}#{_clean(role)}",
        "pk": story_pk(host, repo, number),
        "type": WORK,
        "runId": run_id,
        "role": role,
        "worker": worker,
        "model": model,
        "provider": provider,
        "llmCalls": int(llm_calls or 0),
        "secondsOn429": int(seconds_on_429 or 0),
        "startedAt": started_at,
        "endedAt": ended_at,
        "outcome": outcome,
        "sprintId": sprint_id,
        "updatedAt": now,
    }
    if expires_at:
        doc["expiresAt"] = expires_at
    return doc


def sprint_doc(
    *,
    number: int,
    started_at: str,
    ends_at: str = "",
    committed_stories=None,
    committed_points: int = 0,
    capacity_snapshot=None,
    now: str = "",
) -> dict:
    """A sprint, written once at start and closed at the end.

    `committedStories` is an explicit SNAPSHOT, not something to recompute.
    Asking for every story with this sprintId at the end returns the mid-sprint
    additions too, which makes committed identical to final scope and erases
    the one difference worth measuring. The tester opens issues during a sprint
    and they get worked; that is scope growth, and it has to stay visible.

    `capacitySnapshot` is frozen here for the same reason: computing velocity
    against the CURRENT worker document would rewrite history every time
    somebody adjusts capacity.
    """
    pk = sprint_pk(number)
    return {
        "id": pk,
        "pk": pk,
        "type": SPRINT,
        "number": number,
        "state": "active",
        "startedAt": started_at,
        "endsAt": ends_at,
        "committedStories": list(committed_stories or []),
        "committedPoints": int(committed_points or 0),
        "capacitySnapshot": dict(capacity_snapshot or {}),
        "addedStories": [],
        "addedPoints": 0,
        "completedStories": [],
        "completedPoints": 0,
        "carriedOverStories": [],
        "carriedOverPoints": 0,
        "actualLlmCalls": 0,
        "velocity": None,
        "closedAt": None,
        "updatedAt": now,
    }


def worker_doc(
    *,
    host: str,
    user_id,
    username: str = "",
    role: str = "solver",
    weekly_capacity_points: int = 0,
    now: str = "",
) -> dict:
    """Current configuration for one worker. Deliberately small and mutable.

    Anything historical belongs in the sprint's capacitySnapshot, not here.
    """
    pk = worker_pk(host, user_id)
    return {
        "id": pk,
        "pk": pk,
        "type": WORKER,
        "host": host,
        "userId": user_id,
        "username": username,
        "role": role,
        "weeklyCapacityPoints": int(weekly_capacity_points or 0),
        "updatedAt": now,
    }


def derive_scope(*, sprint_started_at: str, entered_at: str,
                 came_from_sprint=None) -> str:
    """Which of SCOPES this entry is — or "unknown" when it cannot be told.

    Rules, in order:
      - a story arriving from another sprint is `carried`, whenever it arrives
      - otherwise, entering at or before the sprint start is `committed`
      - entering after it is `added`
      - missing either timestamp is `unknown`

    Deliberately string comparison on ISO-8601 UTC timestamps: they sort
    lexicographically, and parsing here would mean this module needs a clock
    and a timezone policy. Callers already produce ISO strings.

    Returning "unknown" rather than assuming "committed" is the point. A
    guessed commitment inflates the sprint's committed scope, which then makes
    the bot look like it under-delivered against a plan it never made.
    """
    if came_from_sprint:
        return "carried"
    if not sprint_started_at or not entered_at:
        return "unknown"
    return "committed" if entered_at <= sprint_started_at else "added"


def enter_sprint(doc: dict, *, sprint_id, scope: str, at: str) -> dict:
    """Move a story into a sprint, recording HOW it got there.

    Returns the same dict, mutated — callers hold the document they are about
    to write. The history entry is appended, never rewritten: a story that
    bounces between sprints keeps every leg of the journey.
    """
    if scope not in SCOPES:
        scope = "unknown"
    doc["sprintId"] = sprint_id
    doc["enteredSprintAt"] = at
    doc["sprintScope"] = scope
    doc.setdefault("sprintHistory", []).append({
        "sprintId": sprint_id,
        "scope": scope,
        "enteredAt": at,
    })
    return doc


def deploy_doc(
    *,
    host: str,
    repo: str,
    sha: str,
    prior_sha: str = "",
    tested_at: str = "",
    outcome: str = "",
    covered_stories=None,
    pull_requests=None,
    sprint_id=None,
    findings: int = 0,
    now: str = "",
) -> dict:
    """One tested commit of a repository's default branch.

    `coveredStories` is what makes the tester's cost attributable at all. The
    tester tests a RANGE (prior_sha..sha); the stories in that range are the
    pull requests that landed in it. Resolving them is the caller's job — it
    needs the GitHub API — but the list belongs here, because it is a fact
    about that commit and does not change afterwards.

    THE COST IS RECORDED ONCE, HERE, AND NOT ON EACH STORY.
    A deploy usually covers several stories. Charging its full cost to every
    one of them would multiply the same tokens by the number of stories in the
    range and make velocity look worse the more efficiently work was batched —
    the exact opposite of the truth. Sprint reporting should therefore sum
    testing as its own line rather than folding it into story points.
    """
    pk = deploy_pk(host, repo, sha)
    return {
        "id": pk,
        "pk": pk,
        "type": DEPLOY,
        "host": host,
        "repo": repo,
        "sha": sha,
        "priorSha": prior_sha,
        "testedAt": tested_at,
        "outcome": outcome,
        # Story keys this commit contained, oldest first. Empty is meaningful:
        # a deploy with no attributable stories is a commit that reached the
        # default branch outside the bot's flow (a human push, a revert), and
        # that is worth being able to see.
        "coveredStories": list(covered_stories or []),
        "pullRequests": list(pull_requests or []),
        "findings": int(findings or 0),
        "sprintId": sprint_id,
        "updatedAt": now,
    }


def tester_work_doc(
    *,
    host: str,
    repo: str,
    sha: str,
    run_id: str,
    worker: str = "",
    model: str = "",
    provider: str = "",
    llm_calls: int = 0,
    seconds_on_429: int = 0,
    started_at: str = "",
    ended_at: str = "",
    outcome: str = "",
    sprint_id=None,
    expires_at: str = "",
    now: str = "",
) -> dict:
    """A tester run, grouped under the DEPLOY key rather than under a story.

    Same shape as work_doc so a sprint can sum both without special-casing;
    only the key differs.
    """
    doc = {
        "id": f"{WORK}#{_clean(run_id)}#tester",
        "pk": deploy_pk(host, repo, sha),
        "type": WORK,
        "runId": run_id,
        "role": "tester",
        "worker": worker,
        "model": model,
        "provider": provider,
        "llmCalls": int(llm_calls or 0),
        "secondsOn429": int(seconds_on_429 or 0),
        "startedAt": started_at,
        "endedAt": ended_at,
        "outcome": outcome,
        "sprintId": sprint_id,
        "updatedAt": now,
    }
    if expires_at:
        doc["expiresAt"] = expires_at
    return doc


def validate(doc: dict) -> list[str]:
    """Problems with a document, or an empty list.

    Called before a write. A malformed document that reaches the store is
    worse than one rejected here: it is queryable, it looks like data, and it
    quietly skews every report built on it.
    """
    problems: list[str] = []
    t = doc.get("type")
    if t not in (STORY, WORK, SPRINT, WORKER, DEPLOY):
        problems.append(f"unknown type {t!r}")
    for field in ("id", "pk"):
        if not doc.get(field):
            problems.append(f"missing {field}")
    pk = doc.get("pk") or ""
    if t == STORY and not pk.startswith(STORY + "#"):
        problems.append(f"story must live under a story key, got {pk!r}")
    if t == DEPLOY and not pk.startswith(DEPLOY + "#"):
        problems.append(f"deploy must live under a deploy key, got {pk!r}")
    if t == WORK and not (pk.startswith(STORY + "#")
                          or pk.startswith(DEPLOY + "#")):
        # A work event belongs with the thing it was work ON: a story for the
        # solver and the reviewer, a tested commit for the tester. Anywhere
        # else and the sprint arithmetic silently misses it.
        problems.append(
            f"work must live under a story or deploy key, got {pk!r}")
    if t == SPRINT and not pk.startswith(SPRINT + "#"):
        problems.append(f"sprint must live under a sprint key, got {pk!r}")
    if t == WORKER and not pk.startswith(WORKER + "#"):
        problems.append(f"worker must live under a worker key, got {pk!r}")
    if t == WORK:
        if doc.get("role") == "tester" and not pk.startswith(DEPLOY + "#"):
            problems.append(
                "a tester work event belongs under a deploy key: the tester "
                "works on a tested commit, not on a story")
        if doc.get("role") not in ROLES:
            problems.append(f"role {doc.get('role')!r} is not one of {ROLES}")
        if int(doc.get("llmCalls") or 0) < 0:
            problems.append("llmCalls is negative")
    if t == STORY and doc.get("host") not in HOSTS:
        problems.append(f"host {doc.get('host')!r} is not one of {HOSTS}")
    if t == STORY and doc.get("origin") not in ORIGINS:
        problems.append(f"origin {doc.get('origin')!r} is not one of {ORIGINS}")
    if t == STORY and doc.get("sprintScope") not in SCOPES:
        problems.append(
            f"sprintScope {doc.get('sprintScope')!r} is not one of {SCOPES}")
    if t == STORY and doc.get("sprintId") and doc.get("sprintScope") == "unknown":
        # Not fatal, but worth surfacing: a story sitting in a sprint with no
        # story about how it got there cannot be counted in any of the three
        # sprint lists, so it will silently vanish from the arithmetic.
        problems.append(
            "story is in a sprint but its sprintScope is unknown — it will not "
            "count as committed, added or carried")
    return problems

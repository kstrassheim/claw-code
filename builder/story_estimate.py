"""story_estimate: what a label on a story MEANS, and where its size lives.

Imported by the issue tick (which decides what to estimate), by
estimate-runner (which produces the number) and by the solver (which picks a
model from it), so the three cannot drift on where an estimate lives — the
same reasoning as issue_priority.py and project_allowlist.py.

WHY THE SIZE LIVES ON THE ISSUE
-------------------------------
The planning store is allowed to be unreachable — that is its whole design,
writes spool and flush later. But the solver has to choose a MODEL before it
starts, and that choice depends on the size. An input that is sometimes absent
cannot drive a decision that always has to be made.

So the issue is the source of truth for the size and the store is the record
of it. A store outage then degrades reporting, not routing. It is also the
only form a human can see and correct without a database client.

THE LABEL IS THE ONLY STORAGE
-----------------------------
Some trackers carry a native integer size field, which their boards and
reports read directly. GitHub has no such field: an issue has a title, a body,
labels, assignees, milestone and state, and nothing that holds a number.

That makes `SP::<n>` the whole storage, not a second copy of it, and it
changes two things. First, there is no field to fall back on when the label is
missing — an unlabelled issue is unestimated, full stop, and the caller plans
it as a defaulted 8 (see story_points.effective). Second, nothing else can
contradict the label, so reading is simpler here than on a tracker where a
field and a label can disagree.

Several other spellings are accepted when READING — `storypoints::`,
`points::`, `size::`, `weight::` — because a repository that already sized its
issues by hand did not use our word for it, and refusing to see those numbers
would re-estimate work somebody has already estimated. Only `SP::<n>` is ever
written, so the vocabulary converges on one spelling without anybody having to
relabel anything.

MUTUAL EXCLUSION IS THIS MODULE'S JOB
-------------------------------------
Some forges enforce one-value-per-scope for `scope::value` labels natively.
GitHub does not — `SP::3` and `SP::8` sit on the same issue quite happily, and
then "how big is this?" has two answers. `label_updates()` therefore always
returns the removals alongside the addition, and every caller must apply both;
on GitHub that is a POST to add and one DELETE per removal, because the issue
PATCH has no add/remove semantics of its own. Reading tolerates the broken
state anyway (`points_of_labels` takes the largest) because refusing to answer
would wedge the planner on an issue a human mislabelled by hand.

Removal is owned by the SCOPE, not by the value: a `size::` label naming
something that is not a number on this scale is still ours to clear, or a
typo'd size would survive every estimate and permanently contradict the real
one. The cost is that a repository using `size::` for t-shirt sizes loses that
label the first time the bot estimates the issue — accepted deliberately,
because the alternative is an issue displaying two different sizes with no way
to tell which one the bot is routing on.

WRITE ONLY ON CHANGE
--------------------
A label write that changes nothing still appends a timeline event, and this
runs every five minutes. `label_updates()` returns empty lists when the issue
already carries the size we were about to write, so an unchanged issue stays
quiet instead of accumulating a heartbeat log no human can read past. "Already
carries" is judged on the scope and the value, not on the exact string, so an
issue labelled `sp::5` by hand is left alone rather than being rewritten to
`SP::5` every tick.

WHY THE OTHER LABELS LIVE HERE TOO
----------------------------------
`estimate`, `model::`, `next sprint` and `approval` are not sizes. They are
here because this is the one module that says what a label on a story MEANS,
and three subsystems read them — the planner, the estimator and the solver.
Splitting them across modules would not make the split anywhere except in the
imports; it would just give the three readers more than one file to disagree
with.
"""

from __future__ import annotations

import re

import story_points

# --- label vocabulary -------------------------------------------------------

# The request-to-estimate label. A REQUEST, not a result: it means "size this,
# I have not". It is removed once a number lands, so the label set says what is
# still outstanding rather than accumulating history.
ESTIMATE_LABEL = "estimate"

# The scope this module WRITES a size in. Upper case because it is read at a
# glance in an issue list, where `SP::5` reads as a size and `sp::5` reads as a
# typo.
SIZE_SCOPE = "SP"

# Scopes accepted when READING a size. Normalised keys (see `_key`), so
# `Story Points::5` and `story-points::5` are the same scope.
#
# `weight::` is here because trackers with a native weight field export it that
# way and people carry the habit across.
SIZE_SCOPES = ("sp", "storypoints", "points", "size", "weight")

# "Not this sprint." The issue stays assigned and stays workable — it is simply
# not IMPLEMENTED while the sprint that deferred it is running.
#
# It does NOT defer estimation. Sizing is what the sprint planning needs in
# order to decide whether the story fits in the next one, so an issue deferred
# out of this sprint is exactly the issue whose size is most worth knowing.
#
# Cleared for assigned issues when a new sprint opens, which is what makes it a
# deferral rather than a permanent park: the label means "later", and without
# the clearing it would quietly mean "never".
NEXT_SPRINT_LABEL = "nextsprint"

# "Nobody merges this but me."
#
# A REQUEST for a human sign-off, like `estimate` is a request for a number —
# except this one is never cleared. The bot neither sets nor removes it: it is
# the one label whose whole purpose is to survive every run of the story, so
# that a merge attempt weeks later still stops at the same gate.
#
# What it does NOT mean: "the bot cannot merge". That case already exists and
# is handled without any label (protected branches, required reviews). This is
# for work that is technically mergeable and that somebody still wants to look
# at first.
APPROVAL_LABEL = "approval"

# The spellings that count as a sign-off gate. Deliberately short: every word
# here is a word a project might already use for something else, and a false
# positive parks a finished PR on a human who was never asked for anything.
# `freigabe` is in because these projects are labelled in German as often as
# not.
APPROVAL_ALIASES = (
    "approval", "approve", "approvalrequired", "needsapproval",
    "requiresapproval", "freigabe",
)

# Labels that pin a story to one model, overriding every other choice.
#
# A bare provider name (`kimi`, `minimax`) or a scoped `model::<id>`. Bare
# names are limited to a known list on purpose: `model::anything` is explicit,
# but a project that happens to label an issue with a word we decided means
# something would have its work silently re-routed.
MODEL_PROVIDERS = ("kimi", "minimax", "mistral")

# `ki` and `ai` and `llm` are what people actually type. All read-only: this
# module never writes a model pin and never clears one, because the pin is a
# human's instruction to the bot and not a fact the bot discovers.
MODEL_SCOPES = ("model", "ki", "llm", "ai")

_NOISE = re.compile(r"[^a-z0-9]+")


def _key(text) -> str:
    """Normalise a label or a fragment of one for comparison.

    Everything that is not a letter or a digit is noise: `Next Sprint`,
    `next-sprint`, `next_sprint` and `NextSprint` are the same instruction
    typed by four people.
    """
    return _NOISE.sub("", str(text or "").strip().lower())


def _scope_and_value(name) -> tuple[str, str]:
    """Split `scope::value` into its parts, both exactly as spelled.

    Splits on the LAST `::` so a value that contains one (nobody should, but
    labels are free text) keeps its tail rather than losing it.
    """
    text = str(name or "").strip()
    if "::" not in text:
        return ("", text)
    scope, _, value = text.rpartition("::")
    return (scope.strip(), value.strip())


def _names(labels) -> list[str]:
    """Every label name in an issue, a label list, or a list of dicts.

    Accepts what the callers actually hold: the issue dict straight from the
    API (`{"labels": [{"name": ...}]}`), the label array on its own, or a plain
    list of strings from a test or a log line.

    NEVER RAISES, and that is load-bearing rather than defensive. Approval is a
    merge gate, so an unreadable label list has to mean "no gate" — see
    `requires_approval`. Every other reader wants the same thing for its own
    reason: a planner that raises on one malformed issue stops planning every
    OTHER issue in the same tick.
    """
    if isinstance(labels, dict):
        labels = labels.get("labels")
    out = []
    try:
        for raw in labels or []:
            name = raw.get("name", "") if isinstance(raw, dict) else raw
            name = str(name or "").strip()
            if name:
                out.append(name)
    except Exception:
        # Whatever we were handed is not a label list. Answering "no labels" is
        # the only answer that cannot make things worse; see the docstring.
        return []
    return out


# --- size -------------------------------------------------------------------


def is_size_label(name) -> bool:
    """True for a label this module owns the size scope of.

    Scope-based, not value-based: `SP::later` is ours to clear even though it
    names no size. See the module docstring on why removal is owned by the
    scope.
    """
    scope, _ = _scope_and_value(name)
    return bool(scope) and _key(scope) in SIZE_SCOPES


def points_of_label(name) -> int | None:
    """The size a single label names, or None.

    Only SCOPED forms count. A bare `5` label would be far too easy to hit by
    accident — plenty of teams label issues `v5` or `Q3` — and a wrong size
    silently routes work to the wrong model.
    """
    scope, value = _scope_and_value(name)
    if not scope or _key(scope) not in SIZE_SCOPES:
        return None
    return story_points.normalise(value)


def points_of_labels(labels) -> int | None:
    """The size carried by a label set, or None if it carries none.

    Takes the LARGEST when several disagree. Two sizes on one issue is a
    mistake rather than an instruction, and resolving it downwards is the
    direction that starves a run — the same argument story_points.normalise
    makes for rounding up.
    """
    best = None
    for name in _names(labels):
        n = points_of_label(name)
        if n is not None and (best is None or n > best):
            best = n
    return best


def points_of(issue) -> int | None:
    """The size of an issue as GitHub holds it, or None if unestimated.

    There is no second place to look: GitHub has no size field, so the label is
    the answer or there is no answer. See the module docstring.
    """
    return points_of_labels(issue)


def effective_points(issue) -> tuple[int, bool]:
    """(points, defaulted) for an issue — never None, always says which.

    The pair matters as much here as in story_points.effective: a defaulted 8
    and a judged 8 route identically today but must not be reported as the same
    fact six weeks later.
    """
    return story_points.effective(points_of(issue))


def label_for(points) -> str:
    """The scoped label naming this size, in the one spelling we write."""
    return f"{SIZE_SCOPE}::{points}"


def _is_wanted_size(name, points: int) -> bool:
    """True when this label already says `points`, whatever its capitalisation.

    Compared on the scope and the value rather than on the string, because
    GitHub treats two label names differing only in case as one label. An issue
    labelled `sp::5` by hand would otherwise be "missing" `SP::5` on every
    tick: we would add a label GitHub resolves to the existing one, then delete
    that same one by its real name, and the issue would lose its size and get
    it back every five minutes.
    """
    scope, value = _scope_and_value(name)
    return _key(scope) == _key(SIZE_SCOPE) and _key(value) == _key(points)


def label_updates(current, points) -> tuple[list[str], list[str]]:
    """(labels to add, labels to remove) to record `points` on an issue.

    `current` is an issue dict, its label array, or a plain list of names.

    Returns two empty lists when the issue already says this size and is not
    still asking to be estimated — the caller then skips the API calls
    entirely, which is what keeps a five-minute tick from writing a timeline
    event every time it looks at an issue it is not changing.

    Removals cover every size-scoped label that is not the one we want plus the
    `estimate` request, so an issue never carries two sizes and never keeps
    asking for something already delivered. Nothing else is ever removed: a
    model pin, an approval gate and a `next sprint` deferral all outlive an
    estimate, and clearing one as a side effect of sizing would silently undo a
    human's instruction.
    """
    n = story_points.normalise(points)
    if n is None:
        # Not a coercible size. story_points.normalise already rounds 4 and 7
        # onto the scale, so reaching here means the caller passed something
        # that is not a positive number at all — a programming error, not a
        # mislabelled issue.
        raise ValueError(f"not a story size: {points!r}")

    have = _names(current)
    wanted = label_for(n)

    add = [] if any(_is_wanted_size(name, n) for name in have) else [wanted]

    remove = []
    for name in have:
        if _is_wanted_size(name, n):
            continue
        if is_size_label(name) or is_estimate_request(name):
            if name not in remove:
                remove.append(name)
    return (add, remove)


# --- the estimate request ---------------------------------------------------


def is_estimate_request(name) -> bool:
    """True when this single label asks for an estimate."""
    _, value = _scope_and_value(name)
    return _key(value) == ESTIMATE_LABEL


def wants_estimate(labels) -> bool:
    """True when the issue is asking to be sized."""
    return any(is_estimate_request(name) for name in _names(labels))


def needs_estimate(labels) -> bool:
    """True when there is a number to produce: asked for, or simply missing.

    Both halves are needed. The request label is how a human says "size this
    again" for an issue that already has a number; the missing size is how the
    ordinary unestimated issue arrives, and nobody labels those.
    """
    return wants_estimate(labels) or points_of_labels(labels) is None


# --- the model pin ----------------------------------------------------------


def is_model_pin(name) -> bool:
    """True for a label that pins this story to a model.

    Read-only, like everything about the pin: this exists so callers can see
    that a label is somebody's routing instruction and leave it alone.
    """
    return bool(model_of_label(name))


def model_of_label(name) -> str:
    """The model a single label names, or "".

    A scoped `model::<id>` (or `ki::`/`llm::`/`ai::`), a full `<vendor>/<id>`,
    or a bare vendor name from MODEL_PROVIDERS.
    """
    text = str(name or "").strip()
    if not text:
        return ""
    scope, value = _scope_and_value(text)
    if scope:
        if _key(scope) in MODEL_SCOPES and value:
            return value
        return ""
    # A full id (`kimi/k3`) is unambiguous; a bare word must be a vendor we
    # know, or every issue labelled `docs` would be routing instructions.
    if "/" in text:
        vendor = text.split("/", 1)[0]
        return text if _key(vendor) in MODEL_PROVIDERS else ""
    return text if _key(text) in MODEL_PROVIDERS else ""


def model_label(labels) -> str:
    """The model this story asks for, or "".

    A vendor name resolves to a real id later, by whoever knows which ids are
    actually available here — this only reads what the issue says.

    Takes the FIRST match rather than the largest or the last: two model labels
    is a contradiction with no sensible resolution, and picking one quietly is
    better than picking neither, because the alternative is a run on a model
    nobody asked for.
    """
    for name in _names(labels):
        model = model_of_label(name)
        if model:
            return model
    return ""


# --- next sprint ------------------------------------------------------------


def is_next_sprint(name) -> bool:
    """True when this single label defers the issue to the next sprint."""
    _, value = _scope_and_value(name)
    return _key(value) == NEXT_SPRINT_LABEL


def deferred_to_next_sprint(labels) -> bool:
    """True when the issue is marked 'not this sprint'.

    Gates IMPLEMENTATION only. Estimation runs on a deferred issue exactly as
    on any other — see NEXT_SPRINT_LABEL.
    """
    return any(is_next_sprint(name) for name in _names(labels))


def next_sprint_labels(labels) -> list[str]:
    """The deferral labels present, exactly as the repository spells them.

    The exact spelling is what makes them removable: GitHub deletes a label
    from an issue by name in the URL, so a `Next Sprint` label cannot be
    removed by asking it to delete `nextsprint`.
    """
    out = []
    for name in _names(labels):
        if is_next_sprint(name) and name not in out:
            out.append(name)
    return out


def sprint_rollover_updates(labels) -> tuple[list[str], list[str]]:
    """(add, remove) to clear the deferral when a new sprint opens.

    Same diff idiom as `label_updates`, and for the same reason: an issue that
    was never deferred must produce no write at all, or every rollover would
    stamp the timeline of every open issue in every permitted repository.
    """
    return ([], next_sprint_labels(labels))


# --- approval ---------------------------------------------------------------


def is_approval_request(name) -> bool:
    """True when this single label demands a human sign-off before merging."""
    _, value = _scope_and_value(name)
    return _key(value) in APPROVAL_ALIASES


def requires_approval(labels) -> bool:
    """True when the issue may only be merged after a human says so.

    FAILS OPEN, deliberately, and this is the one place in the module where
    that direction is chosen on purpose rather than inherited: an unreadable
    label list answers False.

    The reasoning is about blast radius. This gate's job is to hold back a
    handful of stories somebody wants to see first. If a GitHub outage, a
    truncated response or a shape we did not anticipate made it answer True,
    every PR in every repository would stop merging and the bot would look
    thoroughly broken for a reason nobody could see from the issue. Failing
    open loses the gate on the affected story — the human's own branch
    protection and required reviews are still there underneath — and that is
    the smaller failure by a wide margin.

    So this feature can never be the reason a repository stops merging.
    """
    return any(is_approval_request(name) for name in _names(labels))


# --- creating the labels in a fresh repository ------------------------------


def label_definitions() -> list[dict]:
    """The labels this model needs, for creating them in a repo that has none.

    GitHub will not put a label on an issue until the label exists in the
    repository, so anything this module writes has to be creatable from here or
    the first estimate in a fresh repo fails with a 422 nobody will read.

    Sizes are listed for every value on the usable scale plus the ceiling, so
    the set does not have to be re-derived when the ceiling moves. `estimate`,
    `next sprint` and `approval` are listed because a human has to be able to
    APPLY them — creating a label is not the same as putting it on an issue,
    and the bot still never writes the last two.
    """
    out = []
    sizes = tuple(story_points.usable_scale())
    if story_points.SPLIT_ENABLED and story_points.SPLIT_POINTS not in sizes:
        sizes += (story_points.SPLIT_POINTS,)
    for points in sizes:
        too_big = (story_points.SPLIT_ENABLED
                   and points >= story_points.SPLIT_POINTS)
        out.append({
            "name": label_for(points),
            # Sizes share one muted colour on purpose: the number is the
            # information, and five different colours would read as five
            # different KINDS of thing. The ceiling is the exception — it is
            # not a size the bot works, it is a refusal.
            "color": "d4a72c" if too_big else "c5def5",
            "description": (
                f"Too big to start ({points} points) — split it."
                if too_big else
                f"Estimated at {points} story point(s)."),
        })
    out.append({
        "name": ESTIMATE_LABEL, "color": "c5def5",
        "description": "Asks the bot to size this story. Removed once it has."})
    out.append({
        "name": "next sprint", "color": "bfdadc",
        "description": "Not this sprint. Still estimated; cleared at rollover."})
    out.append({
        "name": APPROVAL_LABEL, "color": "d93f0b",
        "description": "A human must approve the merge. The bot never sets or clears this."})
    return out

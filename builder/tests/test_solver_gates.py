"""The solver's gates: which model, which status, and who may merge.

WHY THESE ARE SHELL TESTS
-------------------------
Every decision here lives in fixer-runner.sh, and every one of them is the
kind that is invisible when it goes wrong:

  - **model routing** picks what the run costs. Routing a large story into the
    cheap lane produces a run that dies half-finished, which reads as a model
    that is bad at the task rather than a size that was never known.
  - **status** is what the board says. A status write on a CLOSED issue
    reopens it, five minutes after a human closed it, forever.
  - **the review gate** is what stops unreviewed code landing. It has to be
    keyed on the head SHA, or a verdict about an older commit green-lights
    whatever was pushed after it.
  - **the sign-off gate** must fail OPEN, or one unreadable label list stops
    every merge in every repository for a reason nobody can see.
  - **the conflict retry** is the only wake trigger for a pull request that is
    green and unmergeable. If it does not survive a run that died, the pull
    request sits there forever while the log says "already handled".

The blocks are EXTRACTED from the runner, never copied, so a restructure fails
loudly instead of leaving a test that passes against code nobody ships. `curl`
and `openclaw` are the fakes on PATH; `kubectl` is stubbed per test because
the reviewer switch is a CronJob field.
"""

import json
import os
import shutil
import subprocess
import unittest

from harness import BUILDER, ShellTestCase

RUNNER = "fixer-runner.sh"

BLOCKS = {
    # The helpers that ask the host anything. Named for what they are now —
    # questions through one seam — rather than for the transport they used to
    # be written in.
    "api": ("# -- the code host", "# -- work-item status"),
    "status": ("# -- work-item status", "# -- pull-request facts"),
    "facts": ("# -- pull-request facts", "# -- autonomous review gate"),
    "review": ("# -- autonomous review gate", "# -- human sign-off gate"),
    "approval": ("# -- human sign-off gate", "# -- the merge"),
    "merge": ("# -- the merge", "# -- rebase-conflict retry"),
    "conflict": ("# -- rebase-conflict retry", "# -- escalation"),
    "escalate": ("# -- escalation", "# -- autonomous-review retry"),
    "review_retry": ("# -- autonomous-review retry", "# -- red-CI retry"),
    "ci_red_retry": ("# -- red-CI retry", "# -- issue snapshot"),
    # The story's size and the run's budget are resolved together at the top
    # of the runner, because two things read the size: how long this run may
    # take, and which model implements it. The routing block below consumes
    # what this one produces, so a routing test has to run both.
    "size": ("# The story's SIZE, resolved once, here",
             "# How hard this subsystem thinks."),
    "model": ("# -- model routing", "# -- detect existing PR"),
}

# The Python modules the runner's inline snippets import. Copied into the
# sandbox rather than pointed at through the checkout, so PYTHONPATH is a path
# the sandbox shell can spell.
MODULES = ("approval_release.py", "issue_status.py", "story_estimate.py",
           "story_points.py")


class RunnerBlock(ShellTestCase):
    """Extracts the runner's real blocks and runs them against fake services."""

    def setUp(self):
        super().setUp()
        for name in MODULES:
            shutil.copy(os.path.join(BUILDER, name),
                        os.path.join(self.bin, name))
        self.fixtures = os.path.join(self.home, "fixtures")
        os.makedirs(self.fixtures, exist_ok=True)
        # The runners ask QUESTIONS now, not URLs, so the stand-in is the
        # seam rather than the transport under it. The fake curl stays for
        # the units that still speak HTTP directly.
        self.env["FAKE_FORGE_DIR"] = "$PWD/fixtures"
        self.env["FAKE_FORGE_LOG"] = "$PWD/forge.log"
        self.env["FAKE_CURL_DIR"] = "$PWD/fixtures"
        self.env["FAKE_CURL_LOG"] = "$PWD/curl.log"
        # WHO the host says the one human is. The runner asks for it now
        # instead of splitting `${REPO%%/*}` off the path, because that first
        # segment is a GROUP and a group is not a person to hand work to.
        self.fixture("owner", "creator" + chr(10))
        self.blocks = {}

    def block(self, name):
        """Path (relative to the sandbox) of one extracted block."""
        if name not in self.blocks:
            start, end = BLOCKS[name]
            src = self.extract_block(RUNNER, start, end)
            dst = os.path.join(self.home, f"{name}.sh")
            shutil.move(src, dst)
            self.blocks[name] = f"{name}.sh"
        return self.blocks[name]

    def sources(self, *names):
        return "".join(f'source "$PWD/{self.block(n)}"\n' for n in names)

    def checks(self, state, name="build", sha=None):
        """What CI did on the head, answered consistently.

        Two questions are asked of a commit — the reduction that gates merge,
        and the per-check detail a summary and a fingerprint are built from —
        and a test that answered only one of them would pin a state the
        runner never actually sees.
        """
        sha = sha or getattr(self, "HEAD", "")
        self.fixture(f"checks_{sha}", state)
        self.fixture(f"check-list_{sha}", [{"name": name, "state": state}])

    def fixture(self, slug, payload):
        with open(os.path.join(self.fixtures, slug), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(payload if isinstance(payload, str) else json.dumps(payload))

    def kubectl(self, body):
        """Install a `kubectl` stub. The reviewer switch is a CronJob field."""
        path = os.path.join(self.bin, "kubectl")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def requests(self):
        """Every question the block asked its host, oldest first."""
        path = os.path.join(self.home, "forge.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]

    def asked(self, verb):
        """Calls of one verb. The verb IS the assertion now.

        This replaced grouping by HTTP method, which was always a proxy for
        the question — `POST` meant "wrote something", and which something
        depended on reading the URL. A gate that must not comment is now
        pinned as `asked("comment") == []`, which is what the test means.
        """
        return [r for r in self.requests() if r.split()[0].split("_")[0] == verb]

    def wrote_anything(self):
        """Every call that CHANGES something. Gates are pinned on this."""
        writes = ("comment", "add-labels", "remove-label", "close-issue",
                  "merge", "submit-review", "request-review",
                  "unrequest-review", "react", "create-issue",
                  "comment-on-commit", "ensure-label")
        return [r for r in self.requests()
                if r.split()[0].split("_")[0] in writes]

    def state(self, name):
        path = os.path.join(self.home, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read().strip()

    def preamble(self, **overrides):
        """The variables a block needs, in the shapes the runner sets them."""
        values = {
            "REPO": "o/r",
            "ISSUE_NUM": "7",
            "ISSUE_TITLE": "a task",
            "ISSUE_BODY": "",
            "BOT_LOGIN": "bot",
            "GITHUB_TOKEN": "token",
            "ISSUE_STATE": "open",
            "ISSUE_CLOSED_AS": "",
            "ISSUE_LABELS_JSON": "[]",
            "AGENT_THINKING": "",
        }
        values.update({k: str(v) for k, v in overrides.items()})
        lines = ['export PYTHONPATH="$PWD/bin"', "set -u"]
        lines += [f'{k}={_q(v)}' for k, v in sorted(values.items())]
        lines += [
            # The seam the blocks reach their host through.
            'FORGE=(forge-cli --repo "$REPO")',
            'CR_NOUN="pull request"',
        ]
        lines += [
            'AWAITING_REVIEW_MARKER="$PWD/awaiting-review"',
            'AWAITING_HUMAN_MARKER="$PWD/awaiting-human"',
            'REVIEW_WAIT_TTL="${REVIEW_WAIT_TTL:-7200}"',
            'APPROVAL_ASKED_FILE="$PWD/approval-asked"',
            'MERGE_REFUSED_FILE="$PWD/merge-refused"',
            'APPROVAL_GRANTED_FILE="$PWD/approval-granted"',
            'SYNC_FP_FILE="$PWD/sync-fp"',
            'SYNC_RETRY_FILE="$PWD/sync-retries"',
            'SYNC_RETRY_CAP="${SYNC_RETRY_CAP:-4}"',
            'REVIEW_FP_FILE="$PWD/review-fp"',
            'REVIEW_RETRY_FILE="$PWD/review-retries"',
            'HUMAN_REVIEW_FP_FILE="$PWD/human-review-fp"',
            'HUMAN_REVIEW_RETRY_FILE="$PWD/human-review-retries"',
            'HUMAN_REVIEW_ESCALATED_FILE="$PWD/human-review-escalated"',
            'REVIEW_ESCALATED_FILE="$PWD/review-escalated"',
            'REVIEW_RETRY_CAP="${REVIEW_RETRY_CAP:-4}"',
            'CI_RETRY_FILE="$PWD/ci-red-retries"',
            'CI_RED_ESCALATED_FILE="$PWD/ci-red-escalated"',
            'CI_RED_RETRY_CAP="${CI_RED_RETRY_CAP:-4}"',
            'DEFAULT_BRANCH=main',
            'repo_owner_login() { echo "${REPO%%/*}"; }',
        ]
        return "\n".join(lines) + "\n"


def _q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------


class ModelRouting(RunnerBlock):
    """Which model implements this story — from its size, then from its pin."""

    def setUp(self):
        super().setUp()
        self.conf = os.path.join(self.home, ".openclaw")

    def configure(self, models=None, limits=None, baseline=None):
        if models is not None:
            with open(os.path.join(self.conf, "agent-models.conf"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(models)
        if limits is not None:
            with open(os.path.join(self.conf, "agent-limits.conf"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(limits)
        if baseline is not None:
            with open(os.path.join(self.conf, "runner-model.default"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(baseline)

    def route(self, points=None, labels=(), **env):
        script = (
            self.preamble(ISSUE_LABELS_JSON=json.dumps(list(labels)))
            + ("" if points is None else f"export STORY_POINTS={_q(points)}\n")
            + 'source "$PWD/bin/agent-limits.sh"\n'
            + 'source "$PWD/bin/agent-models.sh"\n'
            + self.sources("size", "model")
            + 'echo "KEY=$SOLVER_MODEL_KEY"\n'
            + 'echo "MODEL=$AGENT_MODEL"\n'
            # $SOLVER_POINTS, not $STORY_POINTS: the raw value is left exactly
            # as the spawner sent it so the planning record cannot be written
            # from a number the bot invented. The WORKING size is what routes.
            + 'echo "POINTS=$SOLVER_POINTS/$POINTS_DEFAULTED"\n'
        )
        rc, out, err = self.sh(script, **env)
        self.assertEqual(rc, 0, out + err)
        return dict(l.split("=", 1) for l in out.splitlines() if "=" in l
                    and l.split("=", 1)[0] in ("KEY", "MODEL", "POINTS"))

    # -- the cheap lane -------------------------------------------------

    def test_a_small_story_uses_the_small_model(self):
        self.configure(models="solver = kimi/k3\nsolver.small = minimax/MiniMax-M3\n",
                       limits="solver.small.max_points = 3\n")
        got = self.route(points=2)
        self.assertEqual(got["KEY"], "solver.small")
        self.assertEqual(got["MODEL"], "minimax/MiniMax-M3")

    def test_a_story_at_the_threshold_still_counts_as_small(self):
        self.configure(models="solver = kimi/k3\nsolver.small = minimax/MiniMax-M3\n",
                       limits="solver.small.max_points = 3\n")
        self.assertEqual(self.route(points=3)["KEY"], "solver.small")

    def test_a_story_over_the_threshold_uses_the_strong_model(self):
        self.configure(models="solver = kimi/k3\nsolver.small = minimax/MiniMax-M3\n",
                       limits="solver.small.max_points = 3\n")
        got = self.route(points=5)
        self.assertEqual(got["KEY"], "solver")
        self.assertEqual(got["MODEL"], "kimi/k3")

    def test_an_unestimated_story_defaults_to_eight_and_never_goes_cheap(self):
        # The threshold is set ABOVE the default on purpose: even then the
        # defaulted size must not qualify. Under-estimating is the expensive
        # direction — it is what makes a run die half-finished — so an unknown
        # story gets the strong model whatever the threshold says.
        self.configure(models="solver = kimi/k3\nsolver.small = minimax/MiniMax-M3\n",
                       limits="solver.small.max_points = 8\n")
        got = self.route(points=None)
        self.assertEqual(got["POINTS"], "8/1")
        self.assertEqual(got["KEY"], "solver")
        self.assertEqual(got["MODEL"], "kimi/k3")

    def test_a_judged_eight_is_routed_the_same_way_and_still_marked_judged(self):
        # Identical routing, different fact. A defaulted 8 and a judged 8 must
        # not be reported as the same thing six weeks later.
        self.configure(models="solver = kimi/k3\nsolver.small = minimax/MiniMax-M3\n",
                       limits="solver.small.max_points = 3\n")
        self.assertEqual(self.route(points=8)["POINTS"], "8/0")

    def test_a_nonsense_size_is_treated_as_unestimated(self):
        self.configure(models="solver = kimi/k3\nsolver.small = minimax/MiniMax-M3\n",
                       limits="solver.small.max_points = 8\n")
        self.assertEqual(self.route(points="three")["POINTS"], "8/1")

    def test_an_unset_small_key_does_not_borrow_the_baseline(self):
        # agent_model would answer with the baseline here and is therefore
        # almost never empty — which would route every small story into a
        # cheap lane nobody configured. Only agent_model_raw can tell.
        self.configure(models="solver = kimi/k3\n",
                       limits="solver.small.max_points = 3\n",
                       baseline="minimax/MiniMax-M3\n")
        got = self.route(points=1)
        self.assertEqual(got["KEY"], "solver")
        self.assertEqual(got["MODEL"], "kimi/k3")

    def test_the_threshold_can_be_switched_off(self):
        self.configure(models="solver = kimi/k3\nsolver.small = minimax/MiniMax-M3\n",
                       limits="solver.small.max_points = off\n")
        self.assertEqual(self.route(points=1)["KEY"], "solver")

    # -- the pin --------------------------------------------------------

    def test_a_model_label_overrides_the_size_based_choice(self):
        self.configure(models="solver = kimi/k3\nsolver.small = minimax/MiniMax-M3\n",
                       limits="solver.small.max_points = 3\n")
        got = self.route(points=1, labels=["model::minimax/MiniMax-M2.7"])
        self.assertEqual(got["MODEL"], "minimax/MiniMax-M2.7")
        self.assertIn("model::", got["KEY"])

    def test_a_bare_vendor_label_resolves_to_a_configured_model(self):
        self.configure(models="solver = kimi/k3\n", limits="")
        self.assertEqual(self.route(points=8, labels=["minimax"])["MODEL"],
                         "minimax/MiniMax-M3")

    def test_an_unresolvable_pin_keeps_the_model_we_would_have_used(self):
        # A story is still worked, just not on the model somebody hoped for.
        # Better than a run that dies at its first request.
        self.configure(models="solver = kimi/k3\n", limits="")
        got = self.route(points=8, labels=["model::nowhere/nothing"])
        self.assertEqual(got["MODEL"], "kimi/k3")

    def test_an_ordinary_label_is_not_read_as_routing(self):
        self.configure(models="solver = kimi/k3\n", limits="")
        self.assertEqual(self.route(points=8, labels=["docs", "bug"])["MODEL"],
                         "kimi/k3")


# ---------------------------------------------------------------------------


class StatusTransitions(RunnerBlock):
    """What the board says — and the one write that must never happen."""

    def setUp(self):
        super().setUp()

    def run_status(self, call, **overrides):
        return self.sh(self.preamble(**overrides)
                       + self.sources("api", "status")
                       + call + "\n")

    def test_pickup_sets_in_progress(self):
        rc, out, err = self.run_status('set_issue_status "in progress"')
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(self.asked("add-labels"), self.requests())
        self.assertIn("→ in progress", out)

    def test_an_issue_already_in_progress_is_left_alone(self):
        # An unchanged write still appends a timeline event, and this runs
        # every five minutes. An issue whose history is a wall of identical
        # label events is an issue nobody can read past.
        rc, out, err = self.run_status(
            'set_issue_status "in progress"',
            ISSUE_LABELS_JSON=json.dumps(["status::in-progress"]))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.wrote_anything(), [], self.requests())

    def test_a_contradictory_status_label_is_removed_as_well_as_added(self):
        # GitHub does not enforce one-value-per-scope, so both halves of the
        # diff have to be applied or "what is the status?" has two answers.
        rc, out, err = self.run_status(
            'set_issue_status "in progress"',
            ISSUE_LABELS_JSON=json.dumps(["status::wont-do"]))
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(self.wrote_anything(), self.requests())
        self.assertTrue(any("status" in r for r in self.asked("remove-label")),
                        self.requests())

    # -- the rule that must never be softened ---------------------------

    def test_a_closed_issue_is_NEVER_given_a_status(self):
        # Writing a `status::` label reopens a closed issue. A human closed
        # this one; a wrapper that quietly undoes that every five minutes is
        # worse than a wrapper that does nothing.
        rc, out, err = self.run_status('set_issue_status "in progress"',
                                       ISSUE_STATE="closed")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.wrote_anything(), [], self.requests())
        self.assertEqual(self.asked("close-issue"), [], self.requests())
        self.assertIn("that would reopen it", out)

    # -- terminal states ------------------------------------------------

    def test_a_delivery_closes_the_issue_as_completed(self):
        rc, out, err = self.run_status("close_issue_as done")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(self.asked("close-issue"), self.requests())
        self.assertIn("(delivered)", out)

    def test_a_revoke_closes_the_issue_as_not_planned(self):
        # The distinction is the whole reason terminal status lives in the
        # close reason: "was this delivered?" has to be answerable afterwards
        # without re-deriving it from the merge history.
        rc, out, err = self.run_status("""close_issue_as "won't do" """)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("(revoked)", out)

    def test_a_duplicate_is_also_not_planned(self):
        rc, out, err = self.run_status("close_issue_as duplicate")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("(revoked)", out)

    def test_an_already_closed_issue_keeps_its_close_reason(self):
        # Re-closing would rewrite the reason a human chose.
        rc, out, err = self.run_status("close_issue_as done",
                                       ISSUE_STATE="closed")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.asked("close-issue"), [], self.requests())

    def test_a_non_terminal_status_is_refused_by_the_close_path(self):
        rc, out, err = self.run_status('close_issue_as "in progress" || echo REFUSED')
        self.assertIn("REFUSED", out)
        self.assertEqual(self.asked("close-issue"), [], self.requests())


# ---------------------------------------------------------------------------


class ReviewGate(RunnerBlock):
    """Nothing merges without a verdict about the commit that is actually open."""

    HEAD = "abc1234abc1234abc1234abc1234abc1234abcd"

    def setUp(self):
        super().setUp()
        self.fixture("change-request_7", {"headSha": self.HEAD,
                                           "mergeable": True,
                                           "draft": False})
        self.fixture("comments_7", [])
        self.fixture("change-request-comments_7", [])
        self.reviewer_active()

    def reviewer_active(self):
        self.kubectl('echo false')

    def reviewer_suspended(self):
        self.kubectl('echo true')

    def reviewer_unreachable(self):
        self.kubectl('exit 1')

    def verdict(self, text, login="bot"):
        self.fixture("change-request-comments_7",
                     [{"author": {"username": login}, "body": text}])

    def gate(self):
        return self.sh(self.preamble()
                       + self.sources("api", "status", "facts", "review")
                       + "if review_gate 7; then echo MAY_MERGE; else echo HELD; fi\n")

    def test_an_approval_for_the_current_head_opens_the_gate(self):
        self.verdict(f"🔎 REVIEW RESULT: APPROVED (sha {self.HEAD})\n\nlooks fine")
        rc, out, err = self.gate()
        self.assertIn("MAY_MERGE", out, out + err)

    def test_a_shortened_sha_in_the_verdict_is_not_the_head(self):
        # The verdict names its own sha, and matching a prefix would let a
        # verdict about one commit vouch for a different one.
        self.verdict("🔎 REVIEW RESULT: APPROVED (sha abc1234)")
        rc, out, _ = self.gate()
        self.assertIn("HELD", out)

    def test_changes_required_holds_the_gate_and_asks_for_nothing(self):
        self.verdict(f"🔎 REVIEW RESULT: CHANGES REQUIRED (sha {self.HEAD})")
        rc, out, _ = self.gate()
        self.assertIn("HELD", out)
        self.assertIn("requires CHANGES", out)
        self.assertEqual([p for p in self.wrote_anything()
                          if "requested_reviewers" in p], [])

    def test_an_approval_for_an_older_commit_does_not_carry(self):
        # A push invalidates a verdict: the reviewer approved code that is no
        # longer what would be merged.
        self.verdict("🔎 REVIEW RESULT: APPROVED (sha 9999999999999999)")
        rc, out, _ = self.gate()
        self.assertIn("HELD", out)

    def test_no_verdict_yet_requests_one_and_records_the_sha(self):
        # The request is a COMMENT plus the per-sha marker, and not a
        # requested-reviewer: GitHub refuses to add a pull request's author as
        # its own reviewer (422), and this bot authors every pull request it
        # opens — so the reviewer finds the work by authorship instead. What
        # must still happen is that the ask is visible ON THE CHANGE REQUEST —
        # where its verdict lands — and the sha it was made for is recorded.
        rc, out, err = self.gate()
        self.assertIn("HELD", out, out + err)
        self.assertTrue(self.asked("comment-on-change-request"),
                        self.requests())
        self.assertEqual(self.asked("comment"), [],
                         "the request belongs on the change request, not the "
                         "issue — its answer is posted nowhere else")
        self.assertEqual(self.state("awaiting-review"), self.HEAD)

    def test_the_request_is_posted_once_per_head_not_once_per_tick(self):
        self.gate()
        first = len(self.asked("comment-on-change-request"))
        self.gate()
        second = len(self.asked("comment-on-change-request"))
        self.assertEqual(first, 1)
        self.assertEqual(second, 1, "asked again for the same head")

    def test_somebody_elses_comment_is_not_a_verdict(self):
        self.verdict(f"🔎 REVIEW RESULT: APPROVED (sha {self.HEAD})", login="a-human")
        rc, out, _ = self.gate()
        self.assertIn("HELD", out)

    def test_a_verdict_with_no_sha_at_all_is_not_a_verdict_for_this_head(self):
        self.verdict("🔎 REVIEW RESULT: APPROVED")
        rc, out, _ = self.gate()
        self.assertIn("HELD", out)

    def test_an_unresolvable_head_holds_the_gate(self):
        os.remove(os.path.join(self.fixtures, "change-request_7"))
        rc, out, _ = self.gate()
        self.assertIn("HELD", out)

    # -- the documented pre-reviewer behaviour --------------------------

    def test_a_suspended_reviewer_lets_green_pull_requests_merge(self):
        # `reviewer stop` is a supported thing to do. When it is off, the
        # solver behaves exactly as it did before the reviewer existed.
        self.reviewer_suspended()
        rc, out, err = self.gate()
        self.assertIn("MAY_MERGE", out, out + err)
        self.assertIn("suspended", out)
        self.assertEqual(self.wrote_anything(), [], self.requests())

    def test_a_suspended_reviewer_clears_a_wait_left_over_from_before(self):
        with open(os.path.join(self.home, "awaiting-review"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(self.HEAD)
        self.reviewer_suspended()
        self.gate()
        self.assertIsNone(self.state("awaiting-review"))

    def test_an_unreachable_reviewer_fails_open(self):
        # Losing the RBAC to ask about the CronJob must not silently stop
        # every merge in every repository with nothing on the issue to say why.
        self.reviewer_unreachable()
        rc, out, _ = self.gate()
        self.assertIn("MAY_MERGE", out)


# ---------------------------------------------------------------------------


class ApprovalGate(RunnerBlock):
    """A person, not machinery — and it can never be the reason nothing merges."""

    HEAD = "abc1234abc1234abc1234abc1234abc1234abcd"
    LABELS = json.dumps(["approval"])

    def setUp(self):
        super().setUp()
        self.fixture("review-verdicts_7", [])

    def reviews(self, *entries):
        self.fixture("review-verdicts_7", list(entries))

    def gate(self, labels=None, sha=None):
        return self.sh(
            self.preamble(ISSUE_LABELS_JSON=self.LABELS if labels is None else labels)
            + self.sources("api", "status", "approval")
            + f'if approval_gate 7 {_q(sha or self.HEAD)}; then echo MAY_MERGE; '
              'else echo HELD; fi\n')

    def test_no_approval_label_means_no_gate(self):
        rc, out, err = self.gate(labels="[]")
        self.assertIn("MAY_MERGE", out, out + err)
        self.assertEqual(self.wrote_anything(), [], self.requests())

    def test_the_label_holds_the_merge_and_asks_the_owner(self):
        rc, out, err = self.gate()
        self.assertIn("HELD", out, out + err)
        self.assertTrue(self.asked("comment"), self.requests())
        self.assertEqual(self.state("approval-asked"), self.HEAD)

    def test_the_question_is_asked_once_per_head_not_once_per_tick(self):
        self.gate()
        self.gate()
        self.assertEqual(len(self.asked("comment")), 1)

    def test_a_new_head_asks_again(self):
        # An approval covers the code it was given for.
        self.gate()
        self.gate(sha="ffffffffffffffffffffffffffffffffffffffff")
        self.assertEqual(len(self.asked("comment")), 2)

    def test_a_human_approval_for_this_head_opens_the_gate(self):
        self.reviews({"author": "a-human", "verdict": "approved",
                      "sha": self.HEAD})
        rc, out, err = self.gate()
        self.assertIn("MAY_MERGE", out, out + err)
        self.assertEqual(self.state("approval-granted"), self.HEAD)

    def test_the_bots_own_approval_is_not_a_sign_off(self):
        # It is the same account that opened the pull request. An account
        # approving itself is not a human looking at the code.
        self.reviews({"author": "bot", "verdict": "approved",
                      "sha": self.HEAD})
        rc, out, _ = self.gate()
        self.assertIn("HELD", out)

    def test_an_approval_of_an_earlier_commit_does_not_carry(self):
        self.reviews({"author": "a-human", "verdict": "approved",
                      "sha": "0000000000000000000000000000000000000000"})
        rc, out, _ = self.gate()
        self.assertIn("HELD", out)

    def test_a_comment_review_is_not_an_approval(self):
        self.reviews({"author": "a-human", "verdict": "commented",
                      "sha": self.HEAD})
        rc, out, _ = self.gate()
        self.assertIn("HELD", out)

    def test_a_recorded_sign_off_is_not_re_asked(self):
        with open(os.path.join(self.home, "approval-granted"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(self.HEAD)
        rc, out, err = self.gate()
        self.assertIn("MAY_MERGE", out, out + err)
        self.assertEqual(self.wrote_anything(), [], self.requests())

    def test_removing_the_label_clears_a_pending_wait(self):
        with open(os.path.join(self.home, "approval-asked"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(self.HEAD)
        self.gate(labels="[]")
        self.assertIsNone(self.state("approval-asked"))

    # -- the direction this gate fails in -------------------------------

    def test_an_unreadable_label_list_FAILS_OPEN(self):
        # This gate holds back a handful of stories somebody wants to see
        # first. If a truncated response made it answer "yes, a human must
        # sign off", every pull request in every repository would stop merging
        # for a reason invisible from the issue. Failing open loses the gate on
        # the affected story; the repository's own branch protection is still
        # there underneath.
        rc, out, err = self.gate(labels="not json at all")
        self.assertIn("MAY_MERGE", out, out + err)
        self.assertIn("proceeding without a sign-off gate", out)

    def test_an_approval_family_spelling_is_recognised(self):
        for spelling in ("approval", "Approval", "needs approval",
                         "requires-approval", "Freigabe"):
            with self.subTest(spelling=spelling):
                rc, out, _ = self.gate(labels=json.dumps([spelling]))
                self.assertIn("HELD", out, f"{spelling!r} did not gate the merge")


# ---------------------------------------------------------------------------


class MergeOrder(RunnerBlock):
    """The sign-off is asked for LAST, once everything else has said yes."""

    HEAD = "abc1234abc1234abc1234abc1234abc1234abcd"

    def setUp(self):
        super().setUp()
        self.fixture("change-request_7", {"headSha": self.HEAD,
                                           "mergeable": True,
                                           "draft": False})
        self.checks("green")
        self.fixture("change-request-comments_7",
                     [{"author": {"username": "bot"},
                       "body": f"🔎 REVIEW RESULT: APPROVED (sha {self.HEAD})"}])
        self.fixture("comments_7", [])
        self.fixture("review-verdicts_7", [])
        self.kubectl("echo false")

    def merge(self, refuse_merge=False, refusal="", **overrides):
        return self.sh(
            self.preamble(**overrides)
            + self.sources("api", "status", "facts", "review", "approval",
                           "merge", "escalate")
            + 'RUN_OUTCOME=""\n'
            # The fake curl answers a PUT it has no fixture for by falling back
            # to the pull request's own fixture, so refusal is expressed here
            # rather than by deleting a file. Everything else — the gates and
            # the order they run in — is still the runner's own code.
            + (f"merge_pr() {{ MERGE_REFUSAL={_q(refusal)}; return 1; }}\n"
               if refuse_merge else "")
            + "if maybe_merge_green_pr 7; then echo MERGED; else echo NOT_MERGED; fi\n")

    def merged(self):
        return self.asked("merge")

    def test_a_green_reviewed_pull_request_is_merged_and_the_issue_closed(self):
        rc, out, err = self.merge()
        self.assertIn("MERGED", out, out + err)
        self.assertTrue(self.merged(), self.requests())
        self.assertIn("(delivered)", out)

    def test_red_checks_stop_it_before_any_gate_is_consulted(self):
        self.checks("failed")
        rc, out, _ = self.merge()
        self.assertIn("NOT_MERGED", out)
        self.assertEqual(self.merged(), [])
        self.assertIn("checks are 'not_green'", out)

    def test_a_conflicting_branch_is_not_merged(self):
        self.fixture("change-request_7", {"headSha": self.HEAD,
                                           "mergeable": False,
                                           "draft": False})
        rc, out, _ = self.merge()
        self.assertIn("NOT_MERGED", out)
        self.assertIn("conflicts with the base branch", out)

    def test_a_draft_is_not_merged(self):
        self.fixture("change-request_7", {"headSha": self.HEAD,
                                           "mergeable": True,
                                           "draft": True})
        rc, out, _ = self.merge()
        self.assertIn("NOT_MERGED", out)
        self.assertIn("draft", out)

    def test_mergeability_not_computed_yet_is_ask_again_not_no(self):
        # GitHub computes it asynchronously and answers null while thinking.
        self.fixture("change-request_7", {"headSha": self.HEAD,
                                           "draft": False})
        rc, out, _ = self.merge()
        self.assertIn("NOT_MERGED", out)
        self.assertIn("not computed yet", out)

    def test_the_issue_can_opt_out_of_being_merged_by_the_bot(self):
        rc, out, _ = self.merge(ISSUE_BODY="Please do not merge this yourself.")
        self.assertIn("NOT_MERGED", out)
        self.assertEqual(self.merged(), [])

    def test_the_review_gate_stops_it_before_a_human_is_ever_asked(self):
        # Order matters: asking a person to sign off on code the reviewer has
        # not passed spends their attention on something not ready for it.
        self.fixture("comments_7", [])
        self.fixture("change-request-comments_7", [])
        rc, out, _ = self.merge(ISSUE_LABELS_JSON=json.dumps(["approval"]))
        self.assertIn("NOT_MERGED", out)
        self.assertNotIn("MERGE APPROVAL REQUESTED", out)
        self.assertIsNone(self.state("approval-asked"))

    def test_the_sign_off_is_the_last_gate_and_it_stops_a_ready_merge(self):
        rc, out, err = self.merge(ISSUE_LABELS_JSON=json.dumps(["approval"]))
        self.assertIn("NOT_MERGED", out, out + err)
        self.assertEqual(self.merged(), [])
        self.assertEqual(self.state("approval-asked"), self.HEAD)

    def probe(self, script):
        """Run one merge-block function on its own, with its state file local."""
        return self.sh(
            self.preamble()
            + self.sources("api", "status", "facts", "review", "approval",
                           "merge", "escalate")
            + script)

    # -- telling a blip apart from a locked door --------------------------

    def test_merge_refusal_is_permanent_recognises_a_closed_door(self):
        for said in ("HTTP 403 Forbidden", "405 Method Not Allowed",
                     "refusing: protected branch", "review required"):
            rc, out, err = self.probe(
                f'MERGE_REFUSAL={_q(said)}\n'
                'if merge_refusal_is_permanent; then echo SHUT; else echo BLIP; fi\n')
            self.assertIn("SHUT", out, f"{said!r}: {out}{err}")

    def test_merge_refusal_is_permanent_lets_an_ordinary_failure_retry(self):
        rc, out, err = self.probe(
            'MERGE_REFUSAL="502 Bad Gateway"\n'
            'if merge_refusal_is_permanent; then echo SHUT; else echo BLIP; fi\n')
        self.assertIn("BLIP", out, out + err)

    def test_merge_refusal_count_counts_the_same_commit(self):
        rc, out, err = self.probe(
            'merge_refusal_count aaaa; echo; merge_refusal_count aaaa; echo\n')
        self.assertEqual(out.split(), ["1", "2"], out + err)

    def test_merge_refusal_count_starts_over_when_the_head_moves(self):
        # A new commit is a new question: it must not inherit the old one's
        # strikes and park on its first attempt.
        rc, out, err = self.probe(
            'merge_refusal_count aaaa; echo; merge_refusal_count aaaa; echo; '
            'merge_refusal_count bbbb; echo\n')
        self.assertEqual(out.split(), ["1", "2", "1"], out + err)

    def test_merge_refusal_count_survives_having_nowhere_to_write(self):
        # `set -u` is on in the runner. The counter is an optimisation; the
        # merge is not, and an unset state path must not abort the run.
        rc, out, err = self.sh(
            self.preamble()
            + self.sources("api", "status", "facts", "review", "approval",
                           "merge", "escalate")
            + 'unset MERGE_REFUSED_FILE\n'
            + 'merge_refusal_count aaaa; echo\n')
        self.assertEqual(out.split(), ["1"], out + err)

    # -- what happens when the door stays shut ----------------------------

    def test_a_first_refusal_is_retried_not_parked(self):
        rc, out, err = self.merge(refuse_merge=True)
        self.assertIn("retrying next tick", out, out + err)
        self.assertEqual(self.asked("add-labels"), [],
                         "parked an issue on a single transient refusal")

    def test_a_permission_refusal_parks_immediately(self):
        # Retrying a protected branch every five minutes forever is the wrong
        # answer, and a silent one.
        rc, out, err = self.merge(refuse_merge=True,
                                  refusal="403 Forbidden: protected branch")
        self.assertIn("will stay refused", out, out + err)
        self.assertTrue(self.asked("comment"), self.requests())

    def test_the_same_commit_refused_three_times_parks(self):
        for _ in range(3):
            rc, out, err = self.merge(refuse_merge=True)
        self.assertIn("refused 3 times", out, out + err)
        self.assertTrue(self.asked("comment"), self.requests())

    def test_park_merge_blocked_asks_a_person_and_labels_the_issue(self):
        rc, out, err = self.probe(
            'park_merge_blocked 7 abc1234abc1234 "the host said no"\n')
        self.assertTrue(self.asked("comment"), self.requests())
        self.assertTrue(self.asked("add-labels"), self.requests())
        self.assertTrue(os.path.exists(os.path.join(self.home, "awaiting-human")),
                        "parked without the marker the planner ranks on")

    def test_a_refused_merge_leaves_the_pull_request_open(self):
        # Branch protection, a required check the API disagrees about, a race
        # with a human — GitHub can refuse the merge after every gate said yes.
        # Closing the issue then would record a delivery that never happened.
        rc, out, _ = self.merge(refuse_merge=True)
        self.assertIn("NOT_MERGED", out)
        self.assertIn("was refused", out)
        self.assertEqual(self.asked("close-issue"), [],
                         "closed the issue for a merge that never happened")


# ---------------------------------------------------------------------------


class ConflictRetry(RunnerBlock):
    """The one blocker no other wake trigger can see."""

    HEAD = "abc1234abc1234abc1234abc1234abc1234abcd"

    def setUp(self):
        super().setUp()
        self.dirty()

    def dirty(self, sha=None):
        self.fixture("change-request_7",
                     {"headSha": sha or self.HEAD,
                      # The neutral record answers three ways: True, False,
                      # and None for "the host has not decided yet". False is
                      # a conflict; None is not, and reading it as one makes
                      # every freshly-pushed head look broken.
                      "mergeable": False, "draft": False})

    def clean(self):
        self.fixture("change-request_7",
                     {"headSha": self.HEAD,
                      "mergeable": True, "draft": False})

    def check(self, cap=4):
        return self.sh(self.preamble()
                       + f"SYNC_RETRY_CAP={cap}\n"
                       + self.sources("api", "status", "facts", "conflict")
                       + "if conflict_needs_agent 7; then echo WAKE; else echo SLEEP; fi\n")

    def test_a_mergeable_pull_request_wakes_nobody(self):
        self.clean()
        rc, out, err = self.check()
        self.assertIn("SLEEP", out, out + err)

    def test_a_new_conflict_wakes_the_agent(self):
        rc, out, err = self.check()
        self.assertIn("WAKE", out, out + err)
        self.assertIn("new rebase conflict", out)
        self.assertEqual(self.state("sync-retries"), "1")

    def test_the_same_conflict_wakes_the_agent_again(self):
        # THE BUG THIS EXISTS FOR. The fingerprint is written when the conflict
        # is OBSERVED, before the agent has done anything about it — so a run
        # woken and then killed (a 429, a deploy, an OOM) spent the trigger for
        # good. Every later tick found the pair identical, declined to wake,
        # and the pull request sat conflicted forever while the log said,
        # reasonably, "already handled".
        self.check()
        rc, out, err = self.check()
        self.assertIn("WAKE", out, out + err)
        self.assertIn("attempt 2/4", out)
        self.assertEqual(self.state("sync-retries"), "2")

    def test_the_retries_are_bounded(self):
        for _ in range(4):
            self.assertIn("WAKE", self.check()[1])
        rc, out, _ = self.check()
        self.assertIn("SLEEP", out)
        self.assertIn("a human should look", out)

    def test_a_new_head_resets_the_budget(self):
        # Either end moving produces a new fingerprint, so a conflict that WAS
        # worked on costs nothing extra.
        for _ in range(4):
            self.check()
        self.assertIn("SLEEP", self.check()[1])
        self.dirty(sha="fedcba9876543210fedcba9876543210fedcba98")
        rc, out, _ = self.check()
        self.assertIn("WAKE", out)
        self.assertEqual(self.state("sync-retries"), "1")

    def test_resolving_the_conflict_clears_the_state(self):
        self.check()
        self.clean()
        self.check()
        self.assertIsNone(self.state("sync-fp"))
        self.assertIsNone(self.state("sync-retries"))

    def test_a_corrupt_retry_counter_does_not_wedge_the_wake(self):
        self.check()
        with open(os.path.join(self.home, "sync-retries"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("not a number")
        rc, out, _ = self.check()
        self.assertIn("WAKE", out)


if __name__ == "__main__":
    unittest.main()

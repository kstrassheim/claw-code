"""A closed door is not a bad turn, and must not be retried behind a lock.

The runner treated every non-zero agent turn identically: log it, fall into
the poll loop, sleep 300 seconds, repeat. That is right for a timeout or a
flaky call. It is wrong for a quota or credential failure, which will answer
403 to the next call and the one after that.

The cost was not the wasted turn. The run holds the REPOSITORY'S LOCK while it
polls, and there is one slot per repository — so one issue pinned to an
exhausted model starved every other issue in that repo, indefinitely, and the
only symptom was that the bot appeared to have stopped. Observed on
k8s-ultimate-web-stack#88: kimi out of quota, `next=none` because a `model::`
label overrides the fallback, nineteen minutes of holding the lock and still
counting when a human went looking.

The matcher is deliberately NARROW, because the two mistakes do not cost the
same. Abandoning a run on a transient error costs one tick. Mistaking a fatal
error for a transient one costs the whole repository until somebody notices.
So it matches only what the MODEL LAYER says is unusable, never a 403 that
some tool the agent called happened to return.
"""

import os
import re
import subprocess
import tempfile
import unittest

BUILDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(BUILDER, "fixer-runner.sh")


def extract(name: str) -> str:
    """One shell function, lifted out of the runner by name.

    Extracted rather than copied: a rename or a restructure fails this suite
    loudly instead of leaving it passing against code nobody ships.
    """
    src = open(RUNNER, encoding="utf-8").read()
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", src, re.S | re.M)
    if not m:
        raise AssertionError(f"{name}() not found in fixer-runner.sh")
    return m.group(0)


class FatalAgentError(unittest.TestCase):
    def verdict(self, text: str) -> bool:
        """True when the runner would give up rather than poll behind a lock."""
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            script = (extract("agent_error_is_fatal")
                      + f'\nif agent_error_is_fatal {path!r}; then echo FATAL; '
                        'else echo RETRY; fi\n')
            out = subprocess.run(["bash", "-c", script], capture_output=True,
                                 text=True, timeout=30).stdout
            return "FATAL" in out
        finally:
            os.unlink(path)

    # -- the closed doors --------------------------------------------------

    def test_a_spent_billing_cycle_is_fatal(self):
        self.assertTrue(self.verdict(
            "403 You've reached your usage limit for this billing cycle. "
            "Your quota will be refreshed in the next cycle."))

    def test_an_auth_failure_with_no_fallback_left_is_fatal(self):
        # The exact line from #88: the model layer has already tried to fail
        # over and has nowhere left to go.
        self.assertTrue(self.verdict(
            "[model-fallback/decision] model fallback decision: "
            "decision=candidate_failed requested=kimi/k3 candidate=kimi/k3 "
            "reason=auth next=none detail=403"))

    def test_a_surfaced_auth_error_is_fatal(self):
        self.assertTrue(self.verdict(
            "[agent/embedded] embedded run failover decision: "
            "stage=assistant decision=surface_error reason=auth from=kimi/k3"))

    def test_an_exhausted_quota_is_fatal(self):
        self.assertTrue(self.verdict("error: quota exceeded for this key"))

    # -- what must still be retried ---------------------------------------

    def test_a_timeout_is_retried(self):
        self.assertFalse(self.verdict(
            "[agent] turn 1 timed out after 1800s"))

    def test_a_rate_limit_is_retried(self):
        # 429 is the provider saying "later", not "never". Giving up on it
        # would abandon work that a minute's wait fixes.
        self.assertFalse(self.verdict(
            "429 Too Many Requests — retrying with backoff"))

    def test_a_forbidden_from_a_tool_the_agent_called_is_retried(self):
        # A 403 from the code host is the agent's problem to handle, not a
        # reason to abandon the repository's slot.
        self.assertFalse(self.verdict(
            "gh: HTTP 403 Forbidden while fetching the check run log"))

    def test_a_failover_that_found_another_model_is_retried(self):
        # It failed over successfully. There is a usable model.
        self.assertFalse(self.verdict(
            "model fallback decision: decision=candidate_failed reason=auth "
            "next=minimax/MiniMax-M3"))

    def test_an_ordinary_failure_is_retried(self):
        self.assertFalse(self.verdict("[agent] turn 1 exited non-zero (1)"))

    def test_nothing_at_all_is_retried(self):
        self.assertFalse(self.verdict(""))


class TheTurnKeepsItsOutput(unittest.TestCase):
    """`run_agent_turn` has to report the AGENT's status, not `tee`'s.

    The output is piped so the reason survives for the matcher above. A naked
    pipeline returns the last command's status, which is `tee` — always zero —
    so every failure would look like success and nothing would ever be judged
    fatal.
    """

    def test_it_returns_the_agents_exit_status_through_the_pipe(self):
        body = extract("run_agent_turn")
        self.assertIn("PIPESTATUS[0]", body)

    def test_the_output_is_kept_for_the_matcher(self):
        body = extract("run_agent_turn")
        self.assertIn("tee", body)

    def test_a_fatal_turn_exits_instead_of_polling(self):
        src = open(RUNNER, encoding="utf-8").read()
        # Both invocations — the first turn and every follow-up — must ask.
        self.assertEqual(src.count("if agent_error_is_fatal"), 2,
                         "every agent turn must be able to give up")
        self.assertEqual(src.count("blocked-no-model"), 2)

    def test_giving_up_runs_the_cleanup_that_frees_the_lock(self):
        # `exit` rather than a bare return, so the EXIT trap releases the
        # agent slot and removes the lock dir. Staying alive is the bug.
        lines = open(RUNNER, encoding="utf-8").read().splitlines()
        self.assertIn("trap on_exit EXIT", lines)
        hits = [n for n, l in enumerate(lines) if "if agent_error_is_fatal" in l]
        self.assertTrue(hits)
        for n in hits:
            # The branch is short; the exit is inside it, not after.
            branch = "\n".join(lines[n:n + 6])
            self.assertIn("exit 0", branch,
                          f"the fatal branch at line {n + 1} keeps the lock")


if __name__ == "__main__":
    unittest.main()

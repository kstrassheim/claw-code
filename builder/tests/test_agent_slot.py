"""The shared model-concurrency gate, and the tester's place in the queue.

The gate was first-come-first-served, and the tester is the worst possible
winner of that race: its run is the longest of the three, so one tester that
starts first can hold a slot for an hour while issues and pull requests queue
behind it.
"""

import os
import unittest

from harness import ShellTestCase

LIB = ". $PWD/bin/agent-slot.sh"

# A PID alone is not enough to hold a slot, by design: after a pod restart a
# recycled PID would make a dead slot look permanently held. The gate also
# requires the owner's cmdline to look like one of this repo's runners, so the
# tests below occupy the slot with fakes/fixer-runner-stub rather than with the
# test shell — seeding it with $$ gets the slot correctly reaped instead.
#
# The stub's name carries the `fixer-runner` substring the gate matches on,
# exactly as the real `fixer-runner.sh` does.
OWNER_MARKER = "fixer-runner"


def hold_a_slot(stub: str = "fixer-runner-stub") -> str:
    """Shell that parks a live, gate-acceptable owner in slot-1.

    The holder's output is redirected to /dev/null, which is not cosmetic: the
    test harness captures the shell's stdout through a pipe, a background child
    inherits that pipe, and `kill`ing the child's wrapper leaves its `sleep`
    holding the write end. The harness then blocks reading until the sleep ends
    of its own accord, so every test using a holder paid the stub's full 30s.
    """
    return (
        f'bash "$PWD/bin/{stub}" 30 >/dev/null 2>&1 &\n'
        "OWNER=$!\n"
        "mkdir -p $HOME/.openclaw/.agent-slots/slot-1\n"
        "echo $OWNER > $HOME/.openclaw/.agent-slots/slot-1/pid\n"
        "echo held > $HOME/.openclaw/.agent-slots/slot-1/owner\n"
        # Wait until the cmdline CONTAINS the marker, not merely until it is
        # readable. Immediately after a fork /proc/<pid>/cmdline exists and is
        # empty, so `-r` was satisfied while the gate — which greps that same
        # file for a runner name — still read the owner as dead and reaped the
        # slot. The test then saw GOT where it expected YIELDED. It only ever
        # tripped under CI load, because that is what widens the window.
        f"until tr '\\0' ' ' < /proc/$OWNER/cmdline 2>/dev/null "
        f"| grep -q {OWNER_MARKER}; do sleep 0.1; done\n")


class Acquiring(ShellTestCase):
    def _slots(self):
        d = os.path.join(self.home, ".openclaw", ".agent-slots")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_high_priority_takes_a_free_slot(self):
        rc, out, _ = self.sh(
            f"{LIB}\nSLOT_NAME=fixer\nacquire_agent_slot && echo GOT")
        self.assertEqual(rc, 0)
        self.assertIn("GOT", out)

    def test_a_slot_is_released(self):
        rc, out, _ = self.sh(
            f"{LIB}\nSLOT_NAME=fixer\nacquire_agent_slot\nrelease_agent_slot\n"
            "ls $HOME/.openclaw/.agent-slots | wc -l")
        self.assertEqual(out.strip().splitlines()[-1], "0")

    def test_two_high_priority_runs_fit_in_two_slots(self):
        rc, out, _ = self.sh(
            f"{LIB}\nMAX_AGENT_SLOTS=2\n"
            "SLOT_NAME=a\nacquire_agent_slot && echo A\n"
            "AGENT_SLOT=''\n"
            "SLOT_NAME=b\nacquire_agent_slot && echo B")
        self.assertIn("A", out)
        self.assertIn("B", out)


class WhichProcessesMayHoldASlot(ShellTestCase):
    """The owner check is what makes the gate safe against PID recycling, and
    it is spelled with THIS repo's runner names.

    A runner missing from that list has its slot reaped out from under it while
    it is still working, which double-books the model concurrency the gate
    exists to hold — and it fails silently, because a reaped slot looks exactly
    like a slot that was legitimately free.
    """

    def _alive(self, stub):
        # Copy the stub to the name under test so the cmdline carries it. The
        # gate reads /proc/<pid>/cmdline, so the NAME is the whole input.
        rc, out, err = self.sh(
            f"cp $PWD/bin/fixer-runner-stub $PWD/bin/{stub}\n"
            f"{LIB}\n"
            f'bash "$PWD/bin/{stub}" 30 >/dev/null 2>&1 &\n'
            "OWNER=$!\n"
            f"until tr '\\0' ' ' < /proc/$OWNER/cmdline 2>/dev/null "
            f"| grep -q {stub}; do sleep 0.1; done\n"
            "_slot_owner_alive $OWNER && echo OWNER || echo NOTOWNER\n"
            "kill $OWNER 2>/dev/null")
        return out.strip().splitlines()[-1]

    def test_each_of_this_repos_runners_is_accepted(self):
        # Both spellings: the repo keeps the .sh suffix, the image drops it
        # (COPY fixer-runner.sh /usr/local/bin/fixer-runner), and the gate has
        # to accept the name the POD actually runs — which is the second one.
        for stub in ("fixer-runner.sh", "tester-runner.sh",
                     "reviewer-runner.sh",
                     "fixer-runner", "tester-runner", "reviewer-runner"):
            with self.subTest(runner=stub):
                self.assertEqual(self._alive(stub), "OWNER")

    def test_an_unrelated_process_is_not_accepted(self):
        # The point of the cmdline check: a recycled PID belonging to anything
        # else must not keep a dead slot alive forever.
        self.assertEqual(self._alive("some-other-process"), "NOTOWNER")

    def test_a_dead_pid_is_not_accepted(self):
        rc, out, _ = self.sh(
            f"{LIB}\n_slot_owner_alive 999999 && echo OWNER || echo NOTOWNER")
        self.assertEqual(out.strip().splitlines()[-1], "NOTOWNER")


class TesterYields(ShellTestCase):
    """Low priority may only start when a slot remains free afterwards."""

    HOLD = hold_a_slot()

    def test_low_priority_starts_when_everything_is_idle(self):
        rc, out, _ = self.sh(
            f"{LIB}\nMAX_AGENT_SLOTS=2\nAGENT_SLOT_RESERVED=1\n"
            "SLOT_PRIORITY=low\nSLOT_NAME=tester\n"
            "acquire_agent_slot && echo GOT || echo YIELDED")
        self.assertIn("GOT", out)

    def test_low_priority_yields_when_one_slot_is_already_taken(self):
        # Two slots, one held: taking the second would leave nothing for the
        # solver or the reviewer, which is exactly what must not happen.
        rc, out, _ = self.sh(
            self.HOLD
            + f"{LIB}\nMAX_AGENT_SLOTS=2\nAGENT_SLOT_RESERVED=1\n"
            "SLOT_PRIORITY=low\nSLOT_NAME=tester\n"
            "acquire_agent_slot && echo GOT || echo YIELDED\n"
            # Report whether the holder was still alive. This test failed once
            # in CI with the gate reaping the slot as stale, and the assertion
            # message ("YIELDED not found") said nothing about why — a dead
            # holder and a broken gate look identical from the outcome alone.
            "kill -0 $OWNER 2>/dev/null && echo HOLDER_ALIVE || echo HOLDER_DIED\n"
            "kill $OWNER 2>/dev/null")
        self.assertIn("HOLDER_ALIVE", out,
                      "the process holding the slot died during the test — "
                      "this says nothing about the gate")
        self.assertIn("YIELDED", out)

    def test_high_priority_takes_that_same_last_slot(self):
        # The mirror image: the reservation exists FOR the solver, so the
        # solver must still get in where the tester was turned away.
        rc, out, _ = self.sh(
            self.HOLD
            + f"{LIB}\nMAX_AGENT_SLOTS=2\nAGENT_SLOT_RESERVED=1\n"
            "SLOT_PRIORITY=high\nSLOT_NAME=fixer\n"
            "acquire_agent_slot && echo GOT || echo YIELDED\n"
            "kill $OWNER 2>/dev/null")
        self.assertIn("GOT", out)

    def test_the_yield_says_why(self):
        rc, out, _ = self.sh(
            f"{LIB}\nMAX_AGENT_SLOTS=1\nAGENT_SLOT_RESERVED=1\n"
            "SLOT_PRIORITY=low\nSLOT_NAME=tester\n"
            "acquire_agent_slot || true")
        self.assertIn("low priority", out)
        self.assertIn("reserved", out)

    def test_low_priority_does_not_sit_and_wait(self):
        # A yield must be immediate. Waiting would hold the tester process
        # open for nothing, and skipping the tick is free.
        import time
        start = time.time()
        self.sh(f"{LIB}\nMAX_AGENT_SLOTS=1\nAGENT_SLOT_RESERVED=1\n"
                "AGENT_SLOT_WAIT=90\nSLOT_PRIORITY=low\nSLOT_NAME=tester\n"
                "acquire_agent_slot || true")
        self.assertLess(time.time() - start, 30)


class StaleSlots(ShellTestCase):
    def test_a_slot_whose_owner_is_gone_is_reclaimed(self):
        # After a pod restart a recycled PID could otherwise make a dead slot
        # look permanently held, wedging every subsystem at once.
        rc, out, _ = self.sh(
            f"{LIB}\nMAX_AGENT_SLOTS=1\n"
            "mkdir -p $HOME/.openclaw/.agent-slots/slot-1\n"
            "echo 999999 > $HOME/.openclaw/.agent-slots/slot-1/pid\n"
            "echo ghost > $HOME/.openclaw/.agent-slots/slot-1/owner\n"
            "SLOT_NAME=fixer\nacquire_agent_slot && echo GOT || echo BLOCKED")
        self.assertIn("GOT", out)
        self.assertIn("reaping", out)

    def test_a_live_owner_keeps_its_slot(self):
        # The other half of the same rule: reaping a slot whose owner is still
        # working is how two runs end up on the model at once.
        rc, out, _ = self.sh(
            hold_a_slot()
            + f"{LIB}\nMAX_AGENT_SLOTS=1\n"
            "SLOT_NAME=fixer\nacquire_agent_slot && echo GOT || echo BLOCKED\n"
            "kill $OWNER 2>/dev/null",
            AGENT_SLOT_WAIT=0)
        self.assertIn("BLOCKED", out)
        self.assertNotIn("reaping", out)


if __name__ == "__main__":
    unittest.main()

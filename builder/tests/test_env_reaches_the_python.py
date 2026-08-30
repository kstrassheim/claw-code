"""An env prefix on the left of a pipe never reaches the python on the right.

WHAT IT COST
The solver's lexical guard asks the issue's own history "did a person already
answer the destructive-change question?", so that a question the PLANNER asked
is not asked a second time by the solver. It was written as

    BOT="$BOT_LOGIN" forge-cli comments --number N | python3 -c "... os.environ['BOT'] ..."

Each side of a pipe is its own command. The prefix set BOT for `forge-cli`,
which never reads it, and the python on the right saw an empty string. Every
author comparison in `lexical_guard.ask_note_id` then failed, the function
returned None for issues whose ask was sitting in plain view, and the answer
came back "no".

So the solver re-asked. On two issues in one evening the planner asked, the
person replied "its ok continue", and the solver posted the identical question
again and re-parked the issue On Hold — while `ask_note_id` re-anchored on the
solver's OWN new ask, so the reply that had already been given no longer
counted and a second one was needed to escape.

Nothing failed. Both halves ran, exited zero, and produced a plausible answer.

WHY A TEST RATHER THAN A CAREFUL READING
The two forms differ by the position of one prefix and read almost the same:

    BOT="$X" cmd | python3 -c '...'      # wrong: BOT reaches cmd
    cmd | BOT="$X" python3 -c '...'      # right: BOT reaches python

The correct form was already in use three lines away in the same file, which
is how sure one can be by reading. This checks it mechanically instead.
"""

import os
import re
import unittest

from harness import BUILDER

RUNNERS = ("fixer-runner.sh", "reviewer-runner.sh", "tester-runner.sh",
           "estimate-runner.sh")

# `NAME="..."` (or NAME=$X) directly before a command, on a line that ends in a
# pipe-continuation — i.e. the assignment decorates the LEFT-hand command.
PREFIXED_LEFT = re.compile(
    r'^[ \t]*(?P<vars>(?:[A-Z_][A-Z0-9_]*=(?:"[^"]*"|\$\{[^}]+\}|\S+)[ \t]+)+)'
    r'(?P<cmd>"\$\{[A-Z_]+\[@\]\}"|[a-z][\w./-]*)[^\n]*\\\n',
    re.MULTILINE)


def read(name: str) -> str:
    with open(os.path.join(BUILDER, name), encoding="utf-8") as f:
        return f.read()


def env_names(prefix: str):
    return set(re.findall(r'([A-Z_][A-Z0-9_]*)=', prefix))


class NoEnvIsStrandedOnTheWrongSideOfAPipe(unittest.TestCase):
    maxDiff = None

    def test_no_runner_sets_env_for_a_command_that_pipes_into_python(self):
        offenders = []
        for name in RUNNERS:
            src = read(name)
            for m in PREFIXED_LEFT.finditer(src):
                tail = src[m.end():m.end() + 400]
                # Only a pipe INTO python matters: the python is what reads
                # os.environ. A prefix on a command piping into awk or grep is
                # someone else's business.
                if not re.match(r'^[ \t]*\|[ \t]*(?:[A-Z_]+=\S+[ \t]+)*python3?\b',
                                tail):
                    continue
                # `NAME="$(cmd | python3 ...)"` is a command SUBSTITUTION
                # capturing the pipeline's output, not a prefix decorating it.
                # The python there reads stdin, not the environment.
                if "$(" in m.group("vars"):
                    continue
                wanted = env_names(m.group("vars"))
                # Already correct if the SAME names are repeated on the python.
                on_python = env_names(
                    re.match(r'^[ \t]*\|[ \t]*((?:[A-Z_]+=\S+[ \t]+)*)',
                             tail).group(1))
                # And only a name the python actually READS can be missed.
                # Anything else is a prefix that happens to sit there for a
                # reason of its own.
                wanted = {v for v in wanted
                          if re.search(r"environ(?:\.get\(|\[)\s*['\"]%s['\"]" % v,
                                       tail)}
                missing = wanted - on_python
                if missing:
                    line = src[:m.start()].count("\n") + 1
                    offenders.append(f"{name}:{line} {sorted(missing)} "
                                     f"set for `{m.group('cmd')}` but read by "
                                     f"the python after the pipe")
        self.assertEqual(offenders, [], (
            "an environment variable is set on the left of a pipe and read by "
            "the python on the right, where it arrives empty. Move the "
            "assignment onto the python: `cmd | NAME=\"$X\" python3 -c ...`"))

    def test_the_pattern_actually_matches_the_broken_form(self):
        # A regex that matched nothing would pass the assertion above forever.
        broken = 'FOO="$BAR" "${FORGE[@]}" comments --number "$N" 2>/dev/null \\\n  | python3 -c "\n"\n'
        self.assertTrue(PREFIXED_LEFT.search(broken),
                        "the detector no longer recognises the bug it exists for")

    def test_the_pattern_accepts_the_correct_form(self):
        ok = '"${FORGE[@]}" comments --number "$N" 2>/dev/null \\\n  | FOO="$BAR" python3 -c "\n"\n'
        m = PREFIXED_LEFT.search(ok)
        # Either it does not match at all, or the names are repeated on the
        # python side — both are fine; what must not happen is a report.
        if m:
            tail = ok[m.end():]
            on_python = env_names(
                re.match(r'^[ \t]*\|[ \t]*((?:[A-Z_]+=\S+[ \t]+)*)',
                         tail).group(1))
            self.assertTrue(env_names(m.group("vars")) <= on_python)


class TheGuardReadsTheBotName(unittest.TestCase):
    """The two functions the incident was actually about."""

    def setUp(self):
        self.src = read("fixer-runner.sh")

    def body(self, fn: str) -> str:
        start = self.src.index(f"{fn}() {{")
        return self.src[start:self.src.index("\n}", start)]

    def test_ask_note_id_hands_bot_to_python(self):
        self.assertRegex(self.body("ask_note_id"),
                         r'\|\s*BOT="\$BOT_LOGIN"\s+python3')

    def test_lexical_ask_answered_hands_bot_to_python(self):
        self.assertRegex(self.body("lexical_ask_answered"),
                         r'\|\s*BOT="\$BOT_LOGIN"\s+python3')

    def test_neither_leaves_it_on_the_forge_call(self):
        for fn in ("ask_note_id", "lexical_ask_answered"):
            with self.subTest(fn=fn):
                self.assertNotRegex(
                    self.body(fn), r'BOT="\$BOT_LOGIN"\s+"\$\{FORGE\[@\]\}"')


if __name__ == "__main__":
    unittest.main()

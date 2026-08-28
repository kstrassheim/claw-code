"""Multi-line prompt strings must close where the prose ends, not mid-sentence.

THE INCIDENT THIS GUARDS
The one-person @-mention rule was added to the solver's prompt as ordinary
prose, and it spelled two examples with bare double quotes:

    account — not "the owners", not "the maintainers", not

That line sits 185 lines inside `INITIAL_PROMPT="`. The first of those quotes
CLOSED the string. What followed parsed as a temporary environment assignment
in front of a bogus command, so `INITIAL_PROMPT` was never set in the shell at
all, and `set -u` killed every run at the turn-1 invocation:

    fixer-runner: line 3018: owners, not the: command not found
    fixer-runner: line 3419: INITIAL_PROMPT: unbound variable

WHY A TEST AND NOT A LINTER
`bash -n` is happy with all of it — the mangled remainder is still valid
syntax — so the usual syntax gate cannot see this class of bug at all. It
reached both environments and sat there: the fixer crash-looped 167 times on
prod and 251 on dev, once every five minutes, each run recording 0 model
calls while the pod, the CronJobs and the locks all reported healthy. Nothing
was down. The bot simply stopped doing anything, silently.

These prompts are hundreds of lines of English written by people who are
thinking about wording, not about shell quoting, and every future edit runs
the same risk. So the invariant is checked mechanically instead of trusted:

    a TOP-LEVEL double-quoted string spanning several lines must be closed by
    a quote that ENDS its line — anything else means bash stopped reading the
    prose somewhere the author did not intend.

WHY ONLY TOP-LEVEL
Strings nested inside `$(...)` are a different animal: the runners embed
`python3 -c "..."` helpers whose own quoting legitimately closes mid-line and
hands back to the surrounding command. Those are code, written by people who
are thinking about quoting. The prose assignments are the ones that bite, so
the scanner tracks command-substitution depth and only judges depth zero.
"""

from __future__ import annotations

import glob
import os
import re
import unittest

from harness import BUILDER

# Below this, a string is an ordinary one-liner and the eye catches a stray
# quote unaided. The bug needs distance to hide in.
MIN_LINES = 3

# What may legitimately follow the closing quote of a multi-line string.
# Everything here is shell, not English: a redirect, a continuation, a closing
# bracket, a concatenated splice, a comment. A tail that starts with a bare
# word is prose bash is about to try to execute — which is the bug.
TAIL_OK = re.compile(r"""^(?:[)\]};|&\\]|\d*[<>]|\#|["'$])""")


def shell_scripts():
    """Every shell script that ships, runners and crons alike."""
    return sorted(glob.glob(os.path.join(BUILDER, "*.sh")))


def double_quoted_spans(text):
    """Walk shell quoting state; yield the top-level double-quoted strings.

    Yields (start_line, end_line, tail) where `tail` is the remainder of the
    closing line after the quote.

    Honours backslash escapes, single-quoted regions, comments, heredoc bodies
    and `$(...)` nesting, because each of those changes what a `"` means and a
    scanner that ignores any of them reports noise instead of bugs.
    """
    spans = []
    stack = []          # 'dq' (inside a double-quoted string) or 'sub' ($(...))
    dq_start = []       # start line of each open double-quoted string
    i, line, n = 0, 1, len(text)
    heredoc = None

    def in_dq():
        return bool(stack) and stack[-1] == "dq"

    while i < n:
        ch = text[i]

        if ch == "\n":
            line += 1
            i += 1
            if heredoc is not None:
                end = text.find("\n", i)
                body = text[i:] if end == -1 else text[i:end]
                if body.strip() == heredoc:
                    heredoc = None
            continue

        if heredoc is not None:
            i += 1
            continue

        if ch == "\\":
            # A backslash escapes the next character INCLUDING a newline —
            # the line continuations these scripts are full of. Skipping the
            # pair without counting that newline drifts every line number
            # reported afterwards, which sends the reader to the wrong place.
            if i + 1 < n and text[i + 1] == "\n":
                line += 1
            i += 2
            continue

        # A command substitution opens a FRESH quoting context, in prose and
        # in code alike.
        if text.startswith("$(", i):
            stack.append("sub")
            i += 2
            continue

        if in_dq():
            if ch == '"':
                stack.pop()
                start = dq_start.pop()
                # Only judge strings that are not themselves inside a $(...).
                if "sub" not in stack:
                    end = text.find("\n", i)
                    tail = text[i + 1:] if end == -1 else text[i + 1:end]
                    spans.append((start, line, tail))
                i += 1
                continue
            i += 1
            continue

        if ch == ")" and stack and stack[-1] == "sub":
            stack.pop()
            i += 1
            continue

        if ch == "'":
            end = text.find("'", i + 1)
            if end == -1:
                break
            line += text.count("\n", i, end)
            i = end + 1
            continue

        if ch == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue

        # Heredoc opener — but NOT a `<<<` herestring, which quotes normally.
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            m = re.match(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", text[i:])
            if m:
                heredoc = m.group(2)
                i += m.end()
                continue

        if ch == '"':
            stack.append("dq")
            dq_start.append(line)
            i += 1
            continue

        i += 1

    return spans


def offenders_in(text):
    """The multi-line strings that stop being a string too early."""
    bad = []
    for start, end, tail in double_quoted_spans(text):
        if end - start + 1 < MIN_LINES:
            continue
        stripped = tail.strip()
        if not stripped or TAIL_OK.match(stripped):
            continue
        bad.append((start, end, stripped))
    return bad


class MultiLineStringsCloseAtEndOfLine(unittest.TestCase):
    def test_no_prompt_string_closes_in_the_middle_of_its_prose(self):
        found = []
        for path in shell_scripts():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for start, end, tail in offenders_in(text):
                found.append(
                    '%s: string opened on line %d closes on line %d with '
                    'prose still following it: %r\n'
                    '    an unescaped double quote inside the string ended it '
                    'early - escape it as \\"' % (
                        os.path.basename(path), start, end, tail[:70]))

        self.assertEqual([], found, "\n\n" + "\n".join(found) + "\n")


class TheScannerCatchesTheBugItWasWrittenFor(unittest.TestCase):
    """A guard that cannot reproduce the incident it commemorates is a guard
    nobody can trust, so the broken line is replayed here verbatim."""

    BROKEN = ('PROMPT="line one\nline two\n'
              'account - not "the owners", not x\nBegin."\n')
    FIXED = ('PROMPT="line one\nline two\n'
             'account - not \\"the owners\\", not x\nBegin."\n')

    def test_the_unescaped_version_is_flagged(self):
        self.assertTrue(offenders_in(self.BROKEN))

    def test_the_escaped_version_is_clean(self):
        self.assertEqual([], offenders_in(self.FIXED))

    def test_embedded_command_substitution_is_not_flagged(self):
        # The shape the runners really use: a multi-line python helper whose
        # own quotes close mid-line inside $(...). Code, not prose.
        ok = 'X="$(python3 -c "\nimport sys\nprint(1)\n" 2>/dev/null)"\n'
        self.assertEqual([], offenders_in(ok))


if __name__ == "__main__":
    unittest.main()

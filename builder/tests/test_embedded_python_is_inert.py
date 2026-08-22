"""Embedded Python must not be able to RUN anything when the shell reads it.

Several runners pipe JSON into `python3 -c "..."`. That snippet sits inside a
DOUBLE-QUOTED shell string, where the shell still interprets:

    `...`        command substitution — the shell RUNS it
    $(...)       command substitution — the shell RUNS it
    ${...}       parameter expansion

So a snippet is read twice: once by the shell, once by Python. Anything the
shell finds interesting is executed before Python ever sees the file.

This is not theoretical, and it is not limited to code. A COMMENT inside such
a snippet, added to explain that a reviewer formats a commit hash in
backticks, contained backticks — and every run of the solver then tried to
execute the hash:

    /usr/local/bin/fixer-runner: line 914: 211f3ea...: command not found

The comment described the hazard and was the hazard. Python comments are not
shell comments; the shell has already finished with the line by then.

The rule is therefore mechanical rather than a matter of care: inside an
embedded snippet, a literal backtick is spelled \\x60, and a shell expansion is
allowed only where it is deliberately passing a value in.
"""

import os
import re
import unittest

from harness import BUILDER

# Runners that embed Python in a double-quoted shell string.
RUNNERS = ("fixer-runner.sh", "reviewer-runner.sh", "tester-runner.sh",
           "estimate-runner.sh", "cron-issue-spawn.sh", "cron-tester-spawn.sh",
           "cron-reviewer-spawn.sh")

# `python3 -c "` ... unescaped closing `"` on its own line.
SNIPPET = re.compile(r'python3 -c "\n(?P<body>.*?)\n"', re.S)


def snippets(path: str):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for m in SNIPPET.finditer(text):
        start_line = text[:m.start("body")].count("\n") + 1
        yield start_line, m.group("body")


class NothingInAnEmbeddedSnippetRuns(unittest.TestCase):
    def test_there_are_snippets_to_check(self):
        # If the extraction stops matching, every assertion below passes while
        # checking nothing at all.
        found = sum(len(list(snippets(os.path.join(BUILDER, r))))
                    for r in RUNNERS if os.path.exists(os.path.join(BUILDER, r)))
        self.assertGreater(found, 3, "no embedded python snippets were found — "
                                     "the extraction has gone stale")

    def test_no_literal_backtick_anywhere_in_a_snippet(self):
        # Including comments. Especially comments.
        for runner in RUNNERS:
            path = os.path.join(BUILDER, runner)
            if not os.path.exists(path):
                continue
            for line_no, body in snippets(path):
                for offset, line in enumerate(body.splitlines()):
                    if "`" in line:
                        self.fail(
                            f"{runner}:{line_no + offset} has a literal "
                            f"backtick inside an embedded python snippet — the "
                            f"shell will run it as command substitution. "
                            f"Spell it \\x60.\n    {line.strip()}")

    def test_no_command_substitution_in_a_snippet(self):
        for runner in RUNNERS:
            path = os.path.join(BUILDER, runner)
            if not os.path.exists(path):
                continue
            for line_no, body in snippets(path):
                for offset, line in enumerate(body.splitlines()):
                    if "$(" in line:
                        self.fail(
                            f"{runner}:{line_no + offset} has $(...) inside an "
                            f"embedded python snippet — the shell runs it "
                            f"before python sees it.\n    {line.strip()}")

    def test_values_come_in_through_the_environment(self):
        # The safe way to pass a value in is an env var read with os.environ,
        # not a shell expansion spliced into the source. This asserts the
        # pattern is actually in use, so the rule above is a rule and not just
        # an absence.
        path = os.path.join(BUILDER, "fixer-runner.sh")
        bodies = [b for _, b in snippets(path)]
        self.assertTrue(
            any("os.environ" in b for b in bodies),
            "no embedded snippet reads os.environ — if values are no longer "
            "passed that way, this rule needs rethinking rather than deleting")


if __name__ == "__main__":
    unittest.main()

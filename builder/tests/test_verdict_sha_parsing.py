"""The solver must read the reviewer's verdict however the agent formatted it.

The verdict line is written BY THE AGENT from a prompt template. The template
says `(sha <head_sha>)`, but a model formats a commit hash as markdown code far
more often than not, so the comment that actually lands reads:

    🔎 REVIEW RESULT: CHANGES REQUIRED (sha `211f3ea...`)

A parser that only accepted the bare form matched nothing, so the sha came back
EMPTY, every verdict compared unequal to the current head, and the solver waited
for a verdict it had already been handed — 113 consecutive ticks on one issue,
each spending zero model calls, while holding the repository's only spawn slot.

Nothing was broken except a regex. That is exactly why this is pinned: the two
sides of this contract are written in different languages, by different
authors — one of them a language model — and only this test makes them agree.
"""

import os
import re
import unittest

from harness import BUILDER

RUNNER = os.path.join(BUILDER, "fixer-runner.sh")


def solver_pattern() -> str:
    """The real regex out of the shipped runner, not a copy of it.

    Anchored to the verdict parser specifically: the runner embeds several
    Python snippets, and lifting "the first re.search" would silently test
    some unrelated pattern and pass while the real one stayed broken.
    """
    with open(RUNNER, encoding="utf-8") as f:
        src = f.read()
    start = src.index("pr_review_verdict()")
    end = src.index("\n}", start)
    block = src[start:end]
    m = re.search(r"re\.search\(r'(?P<pat>.*?sha.*?)',", block, re.S)
    assert m, "the verdict sha pattern was not found in pr_review_verdict()"
    return m.group("pat").replace("\\'", "'")


def parse(first_line: str):
    m = re.search(solver_pattern(), first_line, re.IGNORECASE)
    return m.group(1) if m else None


SHA = "211f3ea67bfb1c52d114301ec552003247d0267a"


class TheShaIsFoundHoweverItIsWritten(unittest.TestCase):
    def test_the_form_the_agent_actually_produced(self):
        # Taken verbatim from the comment that caused the deadlock.
        self.assertEqual(
            parse(f"🔎 REVIEW RESULT: CHANGES REQUIRED (sha `{SHA}`)"), SHA)

    def test_the_form_the_prompt_template_asks_for(self):
        self.assertEqual(
            parse(f"🔎 REVIEW RESULT: APPROVED (sha {SHA})"), SHA)

    def test_other_shapes_a_model_reaches_for(self):
        for line in (
            f"🔎 REVIEW RESULT: APPROVED (sha: `{SHA}`)",
            f'🔎 REVIEW RESULT: APPROVED (sha "{SHA}")',
            f"🔎 REVIEW RESULT: APPROVED (sha  '{SHA}' )",
            f"🔎 REVIEW RESULT: APPROVED (SHA {SHA})",
        ):
            with self.subTest(line=line[:56]):
                self.assertEqual(parse(line), SHA)

    def test_an_abbreviated_sha_still_parses(self):
        self.assertEqual(parse("🔎 REVIEW RESULT: APPROVED (sha `211f3ea`)"),
                         "211f3ea")

    def test_a_line_with_no_sha_yields_nothing(self):
        # Must stay falsy rather than matching something arbitrary: an empty
        # sha is what the solver compares against the head, and a WRONG sha
        # would be worse than none — it would merge against a stale verdict.
        self.assertIsNone(parse("🔎 REVIEW RESULT: APPROVED (no sha here)"))
        self.assertIsNone(parse("🔎 REVIEW RESULT: APPROVED"))

    def test_it_does_not_match_a_short_hex_run(self):
        # Six characters is below the abbreviation floor; accepting it would
        # let prose like "(sha beefed)" pass for a commit.
        self.assertIsNone(parse("🔎 REVIEW RESULT: APPROVED (sha beefed)"))


class TheTwoSidesAgree(unittest.TestCase):
    def test_the_reviewer_emits_what_the_solver_can_read(self):
        # The reviewer's prompt template is the contract's other half. If it
        # is reworded, this fails rather than deadlocking in production.
        with open(os.path.join(BUILDER, "reviewer-runner.sh"), encoding="utf-8") as f:
            reviewer = f.read()
        emitted = re.findall(r"🔎 REVIEW RESULT: (?:APPROVED|CHANGES REQUIRED) \(sha ([^)]+)\)",
                             reviewer)
        self.assertTrue(emitted, "the reviewer no longer emits a (sha ...) verdict line")
        for placeholder in emitted:
            with self.subTest(placeholder=placeholder):
                rendered = placeholder.replace("$HEAD_SHA", SHA).replace("<head_sha>", SHA)
                self.assertEqual(
                    parse(f"🔎 REVIEW RESULT: APPROVED (sha {rendered})"), SHA,
                    f"the solver cannot parse what the reviewer emits: {placeholder!r}")


if __name__ == "__main__":
    unittest.main()

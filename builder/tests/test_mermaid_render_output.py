"""Where mermaid-render puts a chart when nobody says where.

The chat agent renders a burndown or burnup and then SENDS the image. openclaw
only lets a tool read local media from a fixed set of roots — its own tmp dir,
<state>/media, <state>/workspace, canvas, sandboxes — and plain /tmp is not one
of them. With /tmp as the default, every chart failed with "Local media path is
not under an allowed directory": the numbers came back, the chart never did.

A real render needs Chromium, so this pins the default in the script itself.
"""

import os
import re
import unittest

from harness import BUILDER

SCRIPT = os.path.join(BUILDER, "mermaid-render")


def code() -> str:
    with open(SCRIPT, encoding="utf-8") as f:
        return "\n".join(l for l in f.read().splitlines()
                         if not l.lstrip().startswith("#"))


class DefaultOutputLocation(unittest.TestCase):

    def test_the_default_is_under_the_agent_workspace(self):
        self.assertIn(".openclaw/workspace/charts", code())

    def test_plain_tmp_is_no_longer_the_first_choice(self):
        # /tmp survives only as the fallback when the workspace is not
        # writable; it must not be what an ordinary render picks.
        default = re.search(r'OUT="([^"]*/mermaid-[^"]*)"', code())
        self.assertIsNotNone(default, "no default OUT assignment found")
        self.assertTrue(default.group(1).startswith("$CHART_DIR/"),
                        default.group(1))

    def test_an_explicit_out_still_wins(self):
        self.assertRegex(code(), r'if \[ -z "\$OUT" \]; then')


if __name__ == "__main__":
    unittest.main()

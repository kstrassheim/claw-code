"""The Entra login is five executables, and none of them is importable.

`test_shipped_modules` guards the Python half: a module a planner imports is
reachable by `ast`, so a missing COPY line can be derived. Nothing derives
these. They are shell scripts, reached by NAME — by the agent, out of
TOOLS-entra.md — so no static analysis of the repository can notice that the
image stopped installing them.

That is exactly what happened. The forge adoption rewrote the Dockerfile and
dropped all five COPY lines. The scripts stayed in the repository, every test
passed against the checkout, and the deployed pod simply had no `entra-totp`
on PATH: no MFA code could be produced, `az-login-bot` did not exist, and
`az` resolved to the real CLI instead of the policy wrapper — so the one
login flow this deployment forbids was the only one still reachable. The bot
could not sign in to Azure for weeks and nothing anywhere reported an error,
because "command not found" happened inside an agent turn.

So the list is written out by hand, and that is the point rather than
laziness: it cannot be derived, so it has to be pinned.
"""

import os
import re
import unittest

from harness import BUILDER

DOCKERFILE = os.path.join(BUILDER, "Dockerfile")

# script in builder/  ->  where the image must install it.
#
# az-wrapper's destination is the interesting one and is asserted separately
# below: it is installed AS `az`, shadowing /usr/bin/az by PATH precedence.
HELPERS = {
    "entra-totp": "/usr/local/bin/entra-totp",
    "az-login-bot": "/usr/local/bin/az-login-bot",
    "az-browser-sink": "/usr/local/bin/az-browser-sink",
    "aks-login": "/usr/local/bin/aks-login",
    "az-wrapper": "/usr/local/bin/az",
}


def copied() -> dict:
    """{source basename: destination path} for every COPY in the Dockerfile.

    Multi-source form is the normal one here rather than an edge case: this
    image is a single stage and overlayfs stops at 128 layers, so files that
    keep their name share one COPY. A parser that assumed two tokens would
    read `COPY a b c /usr/local/bin/` as "a lands at b" and quietly report
    every file but the last as unshipped.

    A destination ending in `/` is a directory: each source keeps its own
    basename under it. Otherwise the COPY is a rename and there is exactly
    one source.
    """
    with open(DOCKERFILE, encoding="utf-8") as f:
        text = f.read()
    out = {}
    for line in re.findall(r"^COPY\s+(.*?)\s*$", text, re.MULTILINE):
        parts = [t for t in line.split() if not t.startswith("--")]
        if len(parts) < 2:
            continue
        *sources, dest = parts
        for src in sources:
            base = os.path.basename(src)
            out[base] = dest + base if dest.endswith("/") else dest
    return out


class TheEntraHelpersAreInstalled(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.copied = copied()

    def test_the_check_reads_real_copy_lines(self):
        # A regex that matched nothing would pass every assertion below.
        self.assertIn("forge-cli", self.copied)
        self.assertIn("heartbeat-issue-tick.py", self.copied)

    def test_every_helper_exists_in_the_repository(self):
        # A COPY line naming a file that is not there fails the BUILD, which
        # is loud. This asserts the other direction so the two halves of the
        # pin cannot drift apart silently.
        missing = sorted(h for h in HELPERS
                         if not os.path.exists(os.path.join(BUILDER, h)))
        self.assertEqual(missing, [])

    def test_every_helper_has_a_copy_line(self):
        missing = sorted(h for h in HELPERS if h not in self.copied)
        self.assertEqual(missing, [], (
            "these Entra helpers are in the repository but the image never "
            "installs them. Without them the bot cannot produce an MFA code "
            "and cannot sign in to Azure at all — and the only symptom is "
            "'command not found' inside an agent turn."))

    def test_each_one_lands_where_it_is_called_from(self):
        # The names are the interface: TOOLS-entra.md tells the agent to run
        # `entra-totp` and `az-login-bot`, and az-login-bot calls
        # az-browser-sink by absolute path. Installing one under another name
        # would satisfy the test above and still be broken.
        wrong = {h: self.copied[h] for h, dst in HELPERS.items()
                 if h in self.copied and self.copied[h] != dst}
        self.assertEqual(wrong, {})

    def test_the_wrapper_shadows_the_real_az(self):
        # The whole policy rests on this one destination. Installed as
        # `az-wrapper`, PATH would find /usr/bin/az first, a plain `az login`
        # would fall back to device code, and the ban would be documentation
        # with nothing enforcing it.
        self.assertEqual(self.copied.get("az-wrapper"), "/usr/local/bin/az")


if __name__ == "__main__":
    unittest.main()

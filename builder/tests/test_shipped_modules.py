"""A module that is merged but not installed is a subsystem that is dead.

The shared Python here is installed FLAT into /usr/local/bin and imported by
bare name, one `COPY` line per module. Nothing derives that list — so a new
module lands in the repository, every test passes against the checkout, and
the image it is deployed in raises ImportError on the first tick, in a CronJob
log nobody reads. The planner then does nothing at all, which looks exactly
like "there was nothing to do".

The Dockerfile has always promised this check by name. This is it: every
builder module reachable by import from a planner must have a COPY line.
"""

import ast
import os
import re
import unittest

BUILDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(BUILDER, "Dockerfile")

# The scripts a CronJob runs. Everything they can reach has to be installed
# with them.
ENTRY_POINTS = ("heartbeat-issue-tick.py", "reviewer-tick.py",
                "tester-tick.py")

_COPY = re.compile(r"^COPY\b.*?\s(\S+\.py)\s+/usr/local/bin/", re.MULTILINE)


def shipped() -> set[str]:
    """Module names the image installs, without the extension."""
    with open(DOCKERFILE, encoding="utf-8") as f:
        text = f.read()
    return {os.path.basename(m)[:-3] for m in _COPY.findall(text)}


def local_imports(path: str) -> set[str]:
    """Sibling modules a file imports by bare name."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return {n for n in names
            if os.path.exists(os.path.join(BUILDER, f"{n}.py"))}


def reachable() -> set[str]:
    """Every builder module a planner can reach, transitively."""
    seen: set[str] = set()
    queue = [os.path.join(BUILDER, e) for e in ENTRY_POINTS]
    while queue:
        for name in local_imports(queue.pop()):
            if name in seen:
                continue
            seen.add(name)
            queue.append(os.path.join(BUILDER, f"{name}.py"))
    return seen


class EverythingAPlannerNeedsIsInstalled(unittest.TestCase):
    maxDiff = None

    def test_no_module_is_merged_without_a_copy_line(self):
        missing = sorted(reachable() - shipped())
        self.assertEqual(missing, [], (
            "these modules are imported by a planner but never installed "
            "into the image. Add a COPY line to builder/Dockerfile — "
            "without one the planner raises ImportError on every tick and "
            "the subsystem looks merely idle."))

    def test_the_forge_itself_ships(self):
        # Stated on its own because everything else now depends on it: no
        # forge in the image means no planner can ask its host anything.
        self.assertIn("forge", shipped())
        self.assertIn("forge", reachable())

    def test_the_check_reads_real_copy_lines(self):
        # A regex that matched nothing would pass every assertion above.
        self.assertIn("issue_status", shipped())
        self.assertIn("project_allowlist", shipped())


if __name__ == "__main__":
    unittest.main()

"""The CronJob image must carry exactly what the CronJobs run.

The three CronJobs are orchestrators: they find the openclaw pod and do the
real work inside it via `kubectl exec`. What executes in the cron container is
one stdlib-only Python planner and some kubectl calls — measured peak 18 MiB
of Python plus 39 MiB of kubectl — so they run from a small image rather than
the 1.83 GB agent one.

THE FAILURE THIS PREVENTS
A slim image is a file list, and a file list rots. Add an import to a tick
script, forget the COPY, and nothing complains: the agent image still has the
module, the unit tests still pass, and the break happens at 00:05 inside a
CronJob nobody is watching, as an ImportError in a pod that is gone by the
time anyone looks.

So the closure is derived from the SOURCE here, the same way, and compared to
what the Dockerfile actually copies. The list cannot drift silently.
"""

import os
import re
import unittest

from harness import BUILDER

DOCKERFILE = os.path.join(BUILDER, "cron", "Dockerfile")

ENTRYPOINTS = ["cron-issue-spawn.sh", "cron-tester-spawn.sh",
               "cron-reviewer-spawn.sh"]

# Modules that ship with CPython. Anything outside this set has to be COPYed.
STDLIB = set("""
json os re ssl subprocess sys time base64 abc unicodedata urllib datetime
shlex textwrap collections itertools math random string typing pathlib
hashlib tempfile argparse functools dataclasses enum io codecs
""".split())


def closure():
    """Every builder file the three entrypoints reach, transitively."""
    found, queue = set(), []

    def scan(text):
        for name in re.findall(r'^\s*(?:import|from)\s+([A-Za-z_]\w*)',
                               text, re.M):
            queue.append(name)
        for name in re.findall(r'/usr/local/bin/([A-Za-z0-9_-]+)', text):
            queue.append(name)
        # The spawners name their runners WITHOUT a directory so PATH decides,
        # which is what lets a ConfigMap edit reach the cluster without an
        # image rebuild. Reachability has to follow them there, or every
        # entrypoint looks like dead weight the moment the path comes off.
        #
        # Matched in COMMAND POSITION only — `$(name)`, `nohup name`, and the
        # `runner = 'name'` the embedded Python builds the exec line from.
        # A looser scan would count a runner named in a comment as a
        # dependency and demand the cron image carry it.
        for pattern in (r'\$\(([a-z][a-z0-9-]*)\)',
                        r'nohup\s+(?:env\s+\S+\s+)?([a-z][a-z0-9-]*)',
                        r"runner\s*=\s*'([a-z][a-z0-9-]*)'"):
            for name in re.findall(pattern, text):
                queue.append(name)

    for entry in ENTRYPOINTS:
        with open(os.path.join(BUILDER, entry), encoding="utf-8") as fh:
            scan(fh.read())

    while queue:
        name = queue.pop()
        if name in STDLIB or name in found:
            continue
        for candidate in (name + ".py", name):
            path = os.path.join(BUILDER, candidate)
            if os.path.isfile(path):
                found.add(candidate)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    scan(fh.read())
                break
    return found


def copied():
    """Every source file the Dockerfile installs."""
    with open(DOCKERFILE, encoding="utf-8") as fh:
        body = fh.read()
    return set(re.findall(r'^COPY\s+--chmod=\d+\s+(\S+)', body, re.M))


class TheImageCarriesTheWholeClosure(unittest.TestCase):
    def test_nothing_the_crons_import_is_missing(self):
        missing = sorted(closure() - copied() - set(ENTRYPOINTS))
        self.assertEqual(missing, [],
                         f"the cron image would ImportError on: {missing}")

    def test_the_entrypoints_themselves_are_installed(self):
        self.assertEqual(sorted(set(ENTRYPOINTS) - copied()), [])

    def test_nothing_is_carried_that_is_never_reached(self):
        # The point of the image is that it is small. A file nobody imports is
        # either dead weight or evidence the closure is wrong.
        extra = sorted(copied() - closure() - set(ENTRYPOINTS))
        self.assertEqual(extra, [],
                         f"copied but unreachable from the entrypoints: {extra}")

    def test_every_copied_file_exists(self):
        for src in sorted(copied()):
            with self.subTest(src=src):
                self.assertTrue(os.path.isfile(os.path.join(BUILDER, src)))


class TheImageHasWhatTheRuntimeNeeds(unittest.TestCase):
    def setUp(self):
        with open(DOCKERFILE, encoding="utf-8") as fh:
            self.body = fh.read()
        # INSTRUCTIONS ONLY. The header explains what this image deliberately
        # does NOT carry, and names those tools to do it — so a grep over the
        # whole file matches its own documentation and fails for the one
        # reason that is not a problem.
        self.instructions = "\n".join(
            line for line in self.body.splitlines()
            if line.strip() and not line.lstrip().startswith("#"))

    def test_kubectl_is_installed(self):
        # The one binary the entrypoints exec.
        self.assertIn("/usr/local/bin/kubectl", self.instructions)

    def test_ca_certificates_are_present(self):
        # forge.py does its own TLS with urllib + an ssl context. Without a
        # trust store every API call fails verification, and the planner
        # reports an empty world rather than an error.
        self.assertIn("ca-certificates", self.instructions)

    def test_pythonpath_points_at_the_modules(self):
        # The ticks import by bare name, as they do in the agent image.
        self.assertRegex(self.instructions, r"ENV PYTHONPATH=/usr/local/bin")

    def test_it_does_not_run_as_root(self):
        # This container mounts openclaw-secrets for GITHUB_TOKEN.
        self.assertRegex(self.instructions, r"(?m)^USER 1000$")

    def test_the_service_account_does_not_collide_with_a_base_image_user(self):
        """`adduser` fails the BUILD if the name is already taken.

        alpine ships a set of system users, and `cron` is one of them (uid 16)
        — which is the obvious name for this image and the one that broke the
        first build with "user 'cron' in use". Nothing in the Dockerfile hints
        at the collision, so it is checked here instead of being rediscovered.
        """
        alpine_users = {
            "root", "bin", "daemon", "adm", "lp", "sync", "shutdown", "halt",
            "mail", "news", "uucp", "operator", "man", "postmaster", "cron",
            "ftp", "sshd", "at", "squid", "xfs", "games", "cyrus", "vpopmail",
            "ntp", "smmsp", "guest", "nobody",
        }
        for name in re.findall(r"adduser[^\n]*?\s(\S+)\s*$",
                               self.instructions, re.M):
            with self.subTest(user=name):
                self.assertNotIn(name, alpine_users,
                                 f"'{name}' already exists in the base image")

    def test_the_agent_toolchain_is_absent(self):
        # If any of these ever appear here, the image has quietly grown back
        # into the thing it was split out of.
        for tool in ("nodejs", "openclaw", "terraform", "tofu", "dotnet",
                     "powershell", "semgrep", "nuclei", "code-server"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, self.instructions.lower())


if __name__ == "__main__":
    unittest.main()

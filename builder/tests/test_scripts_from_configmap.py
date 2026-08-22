"""The scripts ship as ConfigMaps, and the mount has to actually win.

Almost every commit here changes a script, not a package. Baked into the image
that meant a version bump, a rebuild and a 1.83GB pull for a one-line edit —
minutes of pull on this node, for a file that takes a second to copy. Mounted
from a ConfigMap instead, a script change is Argo applying a ConfigMap and
kubelet refreshing the files in place: no rebuild, no restart, no bump.

THE FAILURE THIS GUARDS IS SILENT, WHICH IS WHY IT IS A TEST
The image keeps its own copies as a fallback, so if the mount stops winning —
a lost PATH entry, a subPath creeping in, a name-suffix hash coming back —
nothing breaks. Everything still runs, just the version from the image, and an
edit appears to do nothing at all. That is far worse than a crash.

The keys are the INSTALLED names (`heartbeat-issue-tick`, not
`heartbeat-issue-tick.py`) so the same file is found under the same name
whichever source wins.
"""

import os
import re
import unittest

from harness import BUILDER

K8S = os.path.join(os.path.dirname(BUILDER), "k8s")
GENERATOR = os.path.join(BUILDER, "kustomization.yaml")

CRONS = ["050-issue-watcher.yaml", "051-tester.yaml", "052-reviewer.yaml"]


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TheGeneratorProducesBothHalves(unittest.TestCase):
    def setUp(self):
        self.src = read(GENERATOR)

    def test_both_configmaps_are_generated(self):
        self.assertIn("name: claw-scripts-planner", self.src)
        self.assertIn("name: claw-scripts-runner", self.src)

    def test_the_name_suffix_hash_is_disabled(self):
        # A content hash in the name changes the pod spec's reference, which
        # ROLLS THE POD — reintroducing the image pull this exists to avoid.
        self.assertIn("disableNameSuffixHash: true", self.src)

    def test_keys_are_the_installed_names(self):
        # The image COPYs heartbeat-issue-tick.py to .../heartbeat-issue-tick.
        # The ConfigMap key has to match, or PATH finds two different names.
        self.assertIn("heartbeat-issue-tick=heartbeat-issue-tick.py", self.src)
        self.assertIn("fixer-runner=fixer-runner.sh", self.src)
        # Modules are imported by name and keep their suffix.
        self.assertIn("forge.py=forge.py", self.src)

    def test_the_runner_libraries_travel_with_the_runners(self):
        # _source_lib resolves relative to the runner's own directory, so a
        # runner mounted from the ConfigMap looks for these beside it.
        runner_half = self.src[self.src.index("name: claw-scripts-runner"):]
        for lib in ("agent-slot.sh", "agent-limits.sh", "agent-models.sh",
                    "agent-thinking.sh", "project-kind.sh",
                    "project-instructions.sh"):
            with self.subTest(lib=lib):
                self.assertIn(lib, runner_half)


class EveryWorkloadPrefersTheMount(unittest.TestCase):
    def workloads(self):
        yield "020-deployment.yaml", read(os.path.join(K8S, "020-deployment.yaml"))
        for c in CRONS:
            yield c, read(os.path.join(K8S, c))

    def test_path_puts_the_mount_first(self):
        for name, src in self.workloads():
            with self.subTest(f=name):
                m = re.search(r'value: "([^"]*?/opt/claw-scripts[^"]*)"', src)
                self.assertIsNotNone(m, f"{name} sets no PATH with the mount")
                self.assertTrue(m.group(1).startswith("/opt/claw-scripts"),
                                f"{name}: image copies would win: {m.group(1)}")

    def test_pythonpath_puts_the_mount_first(self):
        # `import forge` resolves by PYTHONPATH order, not PATH.
        for name, src in self.workloads():
            with self.subTest(f=name):
                self.assertRegex(
                    src, r'name: PYTHONPATH\s*\n\s*value: "/opt/claw-scripts:',
                    f"{name}: modules would come from the image")

    def test_nothing_mounts_the_scripts_with_subpath(self):
        # A subPath mount is a one-time copy and NEVER sees an update — which
        # is precisely what this whole change exists to get.
        for name, src in self.workloads():
            with self.subTest(f=name):
                block = re.search(
                    r"- name: claw-scripts\s*\n\s*mountPath: /opt/claw-scripts\s*\n(\s*subPath:.*)?",
                    src)
                self.assertIsNotNone(block, f"{name} does not mount the scripts")
                self.assertIsNone(block.group(1),
                                  f"{name} mounts the scripts with subPath")

    def test_the_files_are_executable(self):
        # ConfigMap volumes default to 0644 and these are run directly.
        for name, src in self.workloads():
            with self.subTest(f=name):
                self.assertIn("defaultMode: 0755", src)


class TheCronPodsCarryOnlyWhatTheyRun(unittest.TestCase):
    """The split earns its keep here, or it is just two objects instead of one."""

    def test_cron_pods_mount_the_planner_half_only(self):
        for c in CRONS:
            src = read(os.path.join(K8S, c))
            with self.subTest(f=c):
                self.assertIn("name: claw-scripts-planner", src)
                self.assertNotIn("claw-scripts-runner", src,
                                 f"{c} mounts runner scripts it never executes")

    def test_the_gateway_mounts_both(self):
        src = read(os.path.join(K8S, "020-deployment.yaml"))
        self.assertIn("name: claw-scripts-planner", src)
        self.assertIn("name: claw-scripts-runner", src)


if __name__ == "__main__":
    unittest.main()

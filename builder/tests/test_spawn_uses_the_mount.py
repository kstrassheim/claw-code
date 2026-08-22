"""The spawners must not name the image path, or the ConfigMap is decorative.

Everything under builder/ ships twice: baked into the image at
/usr/local/bin, and generated into ConfigMaps that mount at
/opt/claw-scripts, which comes FIRST on PATH in both the cron pods and the
gateway. That ordering is the entire mechanism by which a script edit reaches
the cluster without an image rebuild.

Spawning a runner as `/usr/local/bin/fixer-runner` defeats it completely, and
defeats it SILENTLY. The mount is there, the file in it is current, and
nothing executes it. Two fixes shipped that way — `human_review_needs_agent`
and the fatal-error lock release — and both sat in a ConfigMap being correct
and unread, while the pod cheerfully ran the old code from the image. The only
symptom was that the bot kept doing the old thing:

    IMAGE     /usr/local/bin/fixer-runner    human_review_needs_agent: 0
    CONFIGMAP /opt/claw-scripts/fixer-runner human_review_needs_agent: 1

There is a second reason a bare name is the only workable form here, and it is
why the first attempt at this fix was wrong. The spawn command is built inside
a double-quoted `python3 -c "..."`, so a `$VAR` is expanded by the SHELL before
Python ever sees it, and a `"` ends the string outright. Resolving the path
with a shell variable looked right and would have spawned nothing at all.
"""

import os
import re
import unittest

BUILDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SPAWNERS = ("cron-issue-spawn.sh", "cron-tester-spawn.sh",
            "cron-reviewer-spawn.sh")

# What each spawner must reach through PATH rather than by absolute path.
LAUNCHED = ("fixer-runner", "estimate-runner", "tester-runner",
            "reviewer-runner", "heartbeat-issue-tick", "tester-tick",
            "reviewer-tick")


def source(name: str) -> str:
    with open(os.path.join(BUILDER, name), encoding="utf-8") as f:
        return f.read()


def code_only(text: str) -> str:
    """The file with its comment lines dropped.

    The comments explain this rule at length and necessarily quote the path it
    forbids; a test that read them would fail on its own documentation.
    """
    return "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("#"))


class SpawnersUseThePath(unittest.TestCase):
    def test_no_spawner_hardcodes_the_image_path_for_a_runner(self):
        for name in SPAWNERS:
            code = code_only(source(name))
            for prog in LAUNCHED:
                self.assertNotIn(f"/usr/local/bin/{prog}", code,
                                 f"{name} spawns {prog} from the image, so a "
                                 "ConfigMap edit to it can never run")

    def test_the_runners_are_still_actually_launched(self):
        # The rule above is satisfied trivially by launching nothing, so pin
        # that each spawner still names what it starts.
        for name, progs in (("cron-issue-spawn.sh",
                             ("fixer-runner", "estimate-runner",
                              "heartbeat-issue-tick")),
                            ("cron-tester-spawn.sh",
                             ("tester-runner", "tester-tick")),
                            ("cron-reviewer-spawn.sh",
                             ("reviewer-runner", "reviewer-tick"))):
            code = code_only(source(name))
            for prog in progs:
                self.assertIn(prog, code, f"{name} no longer launches {prog}")


class TheSpawnCommandSurvivesItsQuoting(unittest.TestCase):
    """The command is assembled inside a double-quoted `python3 -c "..."`."""

    def embedded(self, name: str) -> str:
        """The python source embedded in the spawner, as the shell hands it over.

        The block opens at `python3 -c "` and closes on a line that is nothing
        but the matching quote, so it is taken from the first to the last —
        anything cleverer trips over the quotes inside it.
        """
        # Comments first: the note explaining this rule quotes the opening
        # `python3 -c "` verbatim, and splitting on the documentation instead
        # of the code is exactly the kind of near-miss this file is about.
        text = code_only(source(name))
        self.assertIn('python3 -c "', text, f"{name}: no embedded python")
        body = text.split('python3 -c "', 1)[1]
        self.assertIn('\n"', body, f"{name}: embedded python is unterminated")
        return body.rsplit('\n"', 1)[0]

    def test_no_shell_variable_survives_into_the_spawn_command(self):
        # `$R` here is expanded by the shell to nothing before Python runs.
        for name in SPAWNERS:
            body = code_only(self.embedded(name))
            for line in body.splitlines():
                if "shlex.quote" in line or "nohup" in line:
                    self.assertNotRegex(
                        line, r'(?<!\\)\$[A-Za-z_]',
                        f"{name}: an unescaped shell variable in the spawn "
                        "command is expanded before Python sees it")

    def test_the_embedded_python_still_parses(self):
        # Whatever quoting games this file plays, what Python receives has to
        # be Python.
        import ast
        for name in SPAWNERS:
            src = self.embedded(name).replace('\\"', '"').replace("\\$", "$")
            try:
                ast.parse(src)
            except SyntaxError as e:
                self.fail(f"{name}: embedded python does not parse: {e}")


class TheCronJobsResolveTheSpawnerToo(unittest.TestCase):
    """The chain is only as good as its first link.

    Fixing the spawners alone would have changed nothing: the CronJob exec'd
    `/usr/local/bin/cron-issue-spawn`, so the IMAGE's spawner ran and spawned
    the IMAGE's runner. Every link has to be resolved through PATH, or the
    ConfigMap is decorative from wherever the absolute path appears downwards.

    Safe by construction: a bare name still resolves when the mount is absent
    or empty, because /usr/local/bin is next on PATH. The failure mode is
    stale, never missing.
    """

    K8S = os.path.join(os.path.dirname(BUILDER), "k8s")

    MANIFESTS = {"050-issue-watcher.yaml": "cron-issue-spawn",
                 "051-tester.yaml": "cron-tester-spawn",
                 "052-reviewer.yaml": "cron-reviewer-spawn"}

    def test_no_cronjob_execs_the_image_copy(self):
        for name, spawner in self.MANIFESTS.items():
            text = open(os.path.join(self.K8S, name), encoding="utf-8").read()
            self.assertNotIn(f'command: ["/usr/local/bin/{spawner}"]', text,
                             f"{name} pins the image copy, so the ConfigMap "
                             "never runs")

    def test_every_cronjob_still_names_its_spawner(self):
        for name, spawner in self.MANIFESTS.items():
            text = open(os.path.join(self.K8S, name), encoding="utf-8").read()
            self.assertIn(f'command: ["{spawner}"]', text)

    def test_the_mount_comes_first_on_path_or_none_of_this_works(self):
        # The whole mechanism is one ordering. If /usr/local/bin were first,
        # every bare name above would resolve to the image and the ConfigMap
        # would be silently ignored again.
        for name in self.MANIFESTS:
            text = open(os.path.join(self.K8S, name), encoding="utf-8").read()
            m = re.search(r"name:\s*PATH\s*\n\s*value:\s*(\S+)", text)
            self.assertIsNotNone(m, f"{name} does not set PATH")
            got = m.group(1).strip("\"'")
            self.assertTrue(got.startswith("/opt/claw-scripts:"),
                            f"{name}: PATH starts {got[:40]}")


if __name__ == "__main__":
    unittest.main()

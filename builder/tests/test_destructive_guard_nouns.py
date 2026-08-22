"""The destructive-intent guard must cover infrastructure and data, not just gates.

Every noun the guard originally protected is a QUALITY GATE or an
OBSERVABILITY signal — tests, lint, CI, coverage, monitoring, auth, backups.
Undoing one of those costs confidence, and the fix is to put it back.

Infrastructure and data are different in kind: nothing re-runs to bring them
back. A dropped table is gone, a destroyed volume is gone, a deleted namespace
takes everything in it. The guard let this straight through as ordinary work:

    "Drop the staging namespace and delete its volumes"

Three destructive verbs and not one protected noun. The verbs were never the
problem — `drop`, `delete` and `remove` were all already there.

WHAT IS DELIBERATELY NOT PROTECTED, and why it is tested
A guard that fires on the REMEDIATION teaches people to ignore it. Two nouns
were tried and removed after checking them against every real issue in these
repositories:

  container — `capabilities: drop: [ALL]` is the recommended pod hardening,
              so it parked every securityContext issue that quoted it.
  secret    — "remove the hardcoded secret" is the fix, not the damage.

Both cases are pinned below so neither comes back.
"""

import unittest

from harness import load


class InfrastructureIsProtected(unittest.TestCase):
    def setUp(self):
        self.g = load("lexical_guard")

    def fires(self, title, body=""):
        return self.g.match(title, body) is not None

    def test_the_issue_that_started_this(self):
        self.assertTrue(self.fires(
            "Drop the staging namespace and delete its volumes",
            "Staging is unused and its persistent volumes are consuming disk."))

    def test_namespaces_clusters_and_nodes(self):
        for t in ("Delete the staging namespace",
                  "Tear down the dev cluster",
                  "Remove the spot node pool"):
            with self.subTest(t=t):
                self.assertTrue(self.fires(t))

    def test_storage(self):
        for t in ("Delete the persistent volume",
                  "Remove the old PVC",
                  "Purge the storage account"):
            with self.subTest(t=t):
                self.assertTrue(self.fires(t))

    def test_terraform_state(self):
        # `destroy` is terraform's own word, so an issue asking for exactly the
        # irreversible thing was phrased in the tool's vocabulary and matched
        # nothing at all before.
        for t in ("Destroy the terraform state",
                  "Delete the tfstate file",
                  "Remove the state file from the backend"):
            with self.subTest(t=t):
                self.assertTrue(self.fires(t))


class DataIsProtected(unittest.TestCase):
    def setUp(self):
        self.g = load("lexical_guard")

    def fires(self, title, body=""):
        return self.g.match(title, body) is not None

    def test_databases_and_tables(self):
        for t in ("Delete the production database",
                  "Drop the users table",
                  "Truncate the events table"):
            with self.subTest(t=t):
                self.assertTrue(self.fires(t))

    def test_object_stores_and_queues(self):
        for t in ("Remove the S3 bucket and its contents",
                  "Purge the dead-letter queue"):
            with self.subTest(t=t):
                self.assertTrue(self.fires(t))


class TheRemediationMustNotBeParked(unittest.TestCase):
    """Both of these were false positives found against real issues."""

    def setUp(self):
        self.g = load("lexical_guard")

    def fires(self, title, body=""):
        return self.g.match(title, body) is not None

    def test_dropping_capabilities_is_hardening_not_destruction(self):
        # k8s-ultimate-web-stack#115, verbatim shape: the recommended fix
        # quotes `capabilities: drop: [ALL]`, and `container` as a protected
        # noun parked every securityContext issue that did.
        self.assertFalse(self.fires(
            "web Deployment has no pod/container securityContext",
            "Set runAsNonRoot, allowPrivilegeEscalation: false and "
            "capabilities: drop: [ALL] on the container."))

    def test_removing_a_hardcoded_secret_is_the_fix(self):
        self.assertFalse(self.fires(
            "Remove the hardcoded secret from the config",
            "It is committed in plaintext; move it to a sealed secret."))


class TheOriginalBehaviourIsUnchanged(unittest.TestCase):
    """The gates the guard already protected, and the things it let through.

    Checked against all 122 real issues in both target repositories when the
    nouns were added: exactly one new match appeared, the one this change is
    for. Everything below is a sample of that, kept as a regression.
    """

    def setUp(self):
        self.g = load("lexical_guard")

    def fires(self, title, body=""):
        return self.g.match(title, body) is not None

    def test_quality_gates_still_fire(self):
        for t in ("Remove all unit tests from the frontend",
                  "Remove the legacy auth tests that keep failing in CI",
                  "Disable the coverage gate"):
            with self.subTest(t=t):
                self.assertTrue(self.fires(t))

    def test_ordinary_work_still_passes(self):
        # Real titles filed against these repositories today.
        for t in ("Add a lint script and run it in the PR pipeline",
                  "Pin the ingress controller chart to an explicit version",
                  "Add a restore path for the nightly MongoDB backup",
                  "Enable authentication on MongoDB",
                  "Add a ResourceQuota and LimitRange per environment namespace",
                  "Validate the kustomize overlays in CI"):
            with self.subTest(t=t):
                self.assertFalse(self.fires(t))

    def test_a_negated_verb_is_still_a_prohibition(self):
        self.assertFalse(self.fires("Do not remove the namespace"))


if __name__ == "__main__":
    unittest.main()

"""Deploying INTO a cluster and PROVISIONING one are different facts.

The `k8s` kind used to MEAN Azure: it required `aks.tf` or `kubernetes.tf` at
the repository root *and* a `k8s/` directory. A project that only ships
manifests — to a k3s box, a homelab, any cluster somebody else runs — matched
nothing and fell through to the `web` default.

k8s-ultimate-web-stack was detected as a WEB APPLICATION. No cluster, no
namespaces, no deployments, no events, no restarts, while it is nothing but
those things. Its Terraform lives in `terraform/`, so even the AKS rule would
not have fired.

The manifests are what make a project Kubernetes. Whether the project also
provisions the cluster is a second, narrower fact, and that is `aks`.
"""

import os
import subprocess
import tempfile
import unittest

from harness import BUILDER

LIB = os.path.join(BUILDER, "project-kind.sh")
TF = 'resource "null_resource" "x" {}\n'


def kinds_for(files):
    """PROJECT_KINDS from the real detector over a throwaway checkout."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        r = subprocess.run(
            ["bash", "-c",
             f'. "{LIB}"; detect_project_kinds_from_dir "{d}"; '
             f'printf "%s" "$PROJECT_KINDS"'],
            capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return sorted(r.stdout.split())


def kinds_from_listing(names):
    """PROJECT_KINDS from a root listing, the way the planner sees a repo."""
    tree = "\n".join(names)
    r = subprocess.run(
        ["bash", "-c",
         f'. "{LIB}"; detect_project_kinds_from_tree "$1"; '
         f'printf "%s" "$PROJECT_KINDS"', "_", tree],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return sorted(r.stdout.split())


class ManifestsAloneAreKubernetes(unittest.TestCase):
    def test_a_local_cluster_project_is_k8s_not_web(self):
        self.assertEqual(kinds_for({"k8s/deploy.yaml": "kind: Deployment\n"}),
                         ["k8s"])

    def test_no_terraform_is_required(self):
        # The whole point: a cluster the project does not provision has no
        # cluster IaC, and demanding some is how this was missed.
        got = kinds_for({"k8s/deploy.yaml": "kind: Deployment\n"})
        self.assertIn("k8s", got)
        self.assertNotIn("aks", got)

    def test_the_planner_sees_it_the_same_way_from_a_listing(self):
        # The planner has only a root listing. If the two disagreed, an issue
        # would be planned as one kind and solved as another.
        self.assertEqual(kinds_from_listing(["k8s", "README.md"]), ["k8s"])

    def test_terraform_in_a_subdirectory_does_not_make_it_azure(self):
        # k8s-ultimate-web-stack: terraform/ holds an app registration, not a
        # cluster. Provisioning an Entra app is not provisioning AKS.
        self.assertEqual(
            kinds_for({"k8s/deploy.yaml": "kind: Deployment\n",
                       "terraform/main.tf": TF,
                       "terraform/app_reg.tf": TF}),
            ["k8s"])


class ClusterIaCMakesItAzure(unittest.TestCase):
    def test_aks_tf_at_the_root_is_aks(self):
        self.assertEqual(
            kinds_for({"k8s/deploy.yaml": "kind: Deployment\n", "aks.tf": TF}),
            ["aks"])

    def test_kubernetes_tf_is_aks_too(self):
        self.assertEqual(
            kinds_for({"k8s/deploy.yaml": "kind: Deployment\n",
                       "kubernetes.tf": TF}),
            ["aks"])

    def test_cluster_iac_one_level_down_still_counts(self):
        # A repository that keeps its cluster IaC in terraform/ provisions a
        # cluster just as much as one that keeps it at the top.
        self.assertEqual(
            kinds_for({"k8s/deploy.yaml": "kind: Deployment\n",
                       "terraform/aks.tf": TF}),
            ["aks"])

    def test_cluster_iac_without_manifests_is_not_a_cluster_workload(self):
        # Nothing is deployed here. It falls back to web, as it always did.
        self.assertEqual(kinds_for({"aks.tf": TF}), ["web"])


class TheHelpersSeparateTheTwoQuestions(unittest.TestCase):
    def ask(self, helper, kinds):
        r = subprocess.run(
            ["bash", "-c",
             f'. "{LIB}"; PROJECT_KINDS="{kinds}"; '
             f'if {helper}; then echo YES; else echo NO; fi'],
            capture_output=True, text=True, timeout=60)
        return r.stdout.strip()

    def test_workload_questions_apply_to_every_cluster(self):
        # What is running, why did it restart, what does this CronJob do — the
        # same facts on a k3s box as on AKS.
        for kinds in ("k8s", "aks", "aksbot"):
            with self.subTest(kinds=kinds):
                self.assertEqual(self.ask("has_cluster_kind", kinds), "YES")

    def test_azure_only_questions_do_not_apply_to_a_local_cluster(self):
        # `az aks`, a node pool, the cluster IaC — a local cluster has none.
        self.assertEqual(self.ask("has_azure_cluster_kind", "k8s"), "NO")
        self.assertEqual(self.ask("has_azure_cluster_kind", "aks"), "YES")
        self.assertEqual(self.ask("has_azure_cluster_kind", "aksbot"), "YES")

    def test_a_local_cluster_is_still_not_a_website(self):
        self.assertEqual(self.ask("has_nonweb_kind", "k8s"), "YES")


class TheBotUpgradeIsAzureOnly(unittest.TestCase):
    def test_aksbot_is_aks_upgraded_never_k8s_upgraded(self):
        # A bot in a cluster the project does not provision is still not an
        # AKS workload, and must not be described as one.
        r = subprocess.run(
            ["bash", "-c",
             f'. "{LIB}"; grep -A 3 "^refine_aks_kind" "{LIB}" | head -8'],
            capture_output=True, text=True, timeout=60)
        self.assertIn("has_kind aks", r.stdout)

    def test_the_labels_no_longer_call_a_local_cluster_aks(self):
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}"; kind_title k8s; kind_label k8s'],
            capture_output=True, text=True, timeout=60)
        self.assertNotIn("AKS", r.stdout)
        self.assertIn("KUBERNETES", r.stdout)

    def test_aks_keeps_its_own_wording(self):
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}"; kind_title aks; kind_label aks'],
            capture_output=True, text=True, timeout=60)
        self.assertIn("AKS", r.stdout)


if __name__ == "__main__":
    unittest.main()

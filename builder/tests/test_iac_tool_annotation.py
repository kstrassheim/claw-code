"""Which of two installed binaries owns a checkout's state.

`tofu` and `terraform` both ship in the image and both read the same `.tf`
files, so the wrong one runs happily. What it does quietly is rewrite the
provider addresses in `.terraform.lock.hcl` — a tool migration nobody asked
for, inside a commit about something else.

The two write different registry hosts, verified on this image:

    tofu init      -> provider "registry.opentofu.org/hashicorp/null"
    terraform init -> provider "registry.terraform.io/hashicorp/null"

Same FILENAME, so the file's presence says nothing; its contents say
everything. Hence evidence (the lockfile) before intent (the pipeline), and
an explicit "undetermined" rather than a guess.

Deliberately an ANNOTATION, not a kind: 14 of 15 repositories here have `.tf`
files, and a kind would fire the reviewer's "MORE THAN ONE THING" preamble on
nearly all of them, which spends the framing that earns its keep on the three
that really are two things.
"""

import os
import subprocess
import tempfile
import unittest

from harness import BUILDER

LIB = os.path.join(BUILDER, "project-kind.sh")


def detect(files):
    """Run the real detector over a throwaway tree. Returns PROJECT_ANNOTATIONS."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        r = subprocess.run(
            ["bash", "-c",
             f'. "{LIB}"; detect_project_annotations_from_dir "{d}"; '
             f'printf "%s" "$PROJECT_ANNOTATIONS"'],
            capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()


TF = 'resource "null_resource" "x" {}\n'


class TheLockfileDecidesWhenThereIsOne(unittest.TestCase):
    def test_opentofu_registry_means_tofu(self):
        self.assertEqual(detect({
            "main.tf": TF,
            ".terraform.lock.hcl": 'provider "registry.opentofu.org/hashicorp/null" {}\n',
        }), "iac-tofu")

    def test_terraform_registry_means_terraform(self):
        self.assertEqual(detect({
            "main.tf": TF,
            ".terraform.lock.hcl": 'provider "registry.terraform.io/hashicorp/null" {}\n',
        }), "iac-terraform")

    def test_the_lockfile_beats_a_pipeline_that_disagrees(self):
        # The lockfile is what the tool actually WROTE; the workflow is what
        # somebody intended. When they disagree, the state on disk wins.
        self.assertEqual(detect({
            "main.tf": TF,
            ".terraform.lock.hcl": 'provider "registry.opentofu.org/hashicorp/null" {}\n',
            ".github/workflows/tf.yml": "  uses: hashicorp/setup-terraform@v3\n",
        }), "iac-tofu")

    def test_a_nested_lockfile_is_found(self):
        # Plenty of repositories keep their IaC in a subdirectory.
        self.assertEqual(detect({
            "terraform/main.tf": TF,
            "terraform/.terraform.lock.hcl": 'provider "registry.terraform.io/hashicorp/null" {}\n',
        }), "iac-terraform")


class ThePipelineDecidesWhenThereIsNoLockfile(unittest.TestCase):
    """k8s-ultimate-web-stack's real shape: .tf files, no lockfile committed."""

    def test_setup_terraform_action(self):
        self.assertEqual(detect({
            "terraform/main.tf": TF,
            ".github/workflows/terraform.yml":
                "      - uses: hashicorp/setup-terraform@v3\n"
                "      - run: terraform init\n",
        }), "iac-terraform")

    def test_setup_opentofu_action(self):
        self.assertEqual(detect({
            "terraform/main.tf": TF,
            ".github/workflows/tf.yml": "      - uses: opentofu/setup-opentofu@v1\n",
        }), "iac-tofu")

    def test_a_bare_tofu_command(self):
        self.assertEqual(detect({
            "main.tf": TF,
            ".gitlab-ci.yml": "script:\n  - tofu init\n  - tofu plan\n",
        }), "iac-tofu")

    def test_the_word_terraform_inside_opentofu_does_not_win(self):
        # `opentofu/setup-opentofu` contains neither "terraform" nor a bare
        # `terraform <verb>`, but a naive substring match on "terraform" would
        # still hit `.terraform.lock.hcl` paths and similar. Pin the ordering.
        self.assertEqual(detect({
            "main.tf": TF,
            ".github/workflows/tf.yml":
                "      - uses: opentofu/setup-opentofu@v1\n"
                "      - run: tofu apply\n",
        }), "iac-tofu")


class SilenceIsNotAnAnswer(unittest.TestCase):
    def test_tf_files_with_no_signal_are_undetermined(self):
        # The dangerous case gets an annotation, not silence.
        self.assertEqual(detect({"main.tf": TF}), "iac-unknown")

    def test_a_repository_with_no_iac_gets_no_annotation(self):
        # There is no question to answer, so nothing is said.
        self.assertEqual(detect({"app.py": "print(1)\n"}), "")

    def test_terraform_build_artifacts_are_not_source(self):
        # .terraform/ is downloaded, not written by a human, and a vendored
        # provider's own .tf files must not make an ordinary repo look like IaC.
        self.assertEqual(detect({
            "app.py": "print(1)\n",
            ".terraform/modules/x/main.tf": TF,
        }), "")


class TheAnnotationRendersSomethingActionable(unittest.TestCase):
    def render(self, files, fn):
        with tempfile.TemporaryDirectory() as d:
            for rel, body in files.items():
                path = os.path.join(d, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
            r = subprocess.run(
                ["bash", "-c",
                 f'. "{LIB}"; detect_project_annotations_from_dir "{d}"; {fn}'],
                capture_output=True, text=True, timeout=60)
            return r.stdout

    def test_the_block_names_the_tool_and_forbids_the_other(self):
        out = self.render({
            "main.tf": TF,
            ".terraform.lock.hcl": 'provider "registry.terraform.io/hashicorp/null" {}\n',
        }, "project_annotations_block")
        self.assertIn("Terraform", out)
        self.assertIn("Do NOT run `tofu`", out)

    def test_the_undetermined_block_forbids_both(self):
        out = self.render({"main.tf": TF}, "project_annotations_block")
        self.assertIn("Run neither", out)

    def test_no_annotations_render_nothing_at_all(self):
        # An empty section with a heading and no content is worse than silence.
        out = self.render({"app.py": "x=1\n"}, "project_annotations_block")
        self.assertEqual(out.strip(), "")

    def test_the_reminder_is_one_line_per_annotation(self):
        # It is re-sent on every solver turn, so it has to stay tiny.
        out = self.render({
            "main.tf": TF,
            ".terraform.lock.hcl": 'provider "registry.terraform.io/hashicorp/null" {}\n',
        }, "project_annotations_reminder")
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("never", lines[0])


class TheAnnotationSetBehavesLikeASet(unittest.TestCase):
    """`_pa_add`, `has_annotation`, `annotation_count`, and the lookups.

    A set rather than a scalar because more annotation families will follow.
    Within one family the values are mutually exclusive — a checkout is managed
    by one binary, not two — and that is enforced by the detector returning at
    its first match, not by anything here, so it is worth pinning that this
    layer would happily hold several.
    """

    def sh(self, script):
        r = subprocess.run(["bash", "-c", f'. "{LIB}"; {script}'],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_adding_is_idempotent(self):
        # The detector may be re-run; a second call must not double an entry.
        self.assertEqual(
            self.sh('_pa_add iac-terraform; _pa_add iac-terraform; '
                    'printf "%s" "$PROJECT_ANNOTATIONS"'),
            "iac-terraform")

    def test_the_set_holds_more_than_one_family(self):
        self.assertEqual(
            self.sh('_pa_add iac-terraform; _pa_add something-else; '
                    'printf "%s|%s" "$PROJECT_ANNOTATIONS" "$(annotation_count)"'),
            "iac-terraform something-else|2")

    def test_has_annotation_answers_both_ways(self):
        self.assertEqual(
            self.sh('_pa_add iac-tofu; '
                    'has_annotation iac-tofu && printf yes || printf no; '
                    'has_annotation iac-terraform && printf ,yes || printf ,no'),
            "yes,no")

    def test_has_annotation_does_not_match_a_prefix(self):
        # " $set " with spaces, so `iac` must not match `iac-tofu`.
        self.assertEqual(
            self.sh('_pa_add iac-tofu; has_annotation iac && printf yes || printf no'),
            "no")

    def test_annotation_count_of_an_empty_set_is_zero(self):
        self.assertEqual(self.sh('printf "%s" "$(annotation_count)"'), "0")

    def test_annotation_title_names_each_case(self):
        self.assertEqual(self.sh('annotation_title iac-terraform'),
                         "Infrastructure tool: Terraform")
        self.assertEqual(self.sh('annotation_title iac-tofu'),
                         "Infrastructure tool: OpenTofu")
        self.assertEqual(self.sh('annotation_title iac-unknown'),
                         "Infrastructure tool: UNDETERMINED")

    def test_an_unknown_title_falls_back_to_the_raw_name(self):
        # Better a bare name than an empty heading — the failure mode that
        # _kind_body has, and that this deliberately does not.
        self.assertEqual(self.sh('annotation_title future-thing'), "future-thing")

    def test_annotation_body_is_empty_for_something_it_does_not_know(self):
        # And the renderer is what must not emit a heading for it.
        self.assertEqual(self.sh('annotation_body future-thing'), "")

    def test_annotation_body_forbids_the_other_binary_in_both_directions(self):
        self.assertIn("Do NOT run `tofu`", self.sh('annotation_body iac-terraform'))
        self.assertIn("Do NOT run `terraform`", self.sh('annotation_body iac-tofu'))


class TheAnnotationNeverReframesThePrompt(unittest.TestCase):
    """The whole reason this is not a kind."""

    def test_detecting_an_annotation_leaves_the_kinds_alone(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "frontend"))
            with open(os.path.join(d, "main.tf"), "w") as fh:
                fh.write(TF)
            r = subprocess.run(
                ["bash", "-c",
                 f'. "{LIB}"; detect_project_kinds_from_dir "{d}"; '
                 f'detect_project_annotations_from_dir "{d}"; '
                 f'printf "%s|%s" "$PROJECT_KINDS" "$(kind_count)"'],
                capture_output=True, text=True, timeout=60)
            kinds, count = r.stdout.split("|")
            self.assertEqual(kinds.strip(), "web")
            self.assertEqual(count.strip(), "1",
                             "an annotation must not push the project multi-kind")


if __name__ == "__main__":
    unittest.main()

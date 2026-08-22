"""Chart output format: png by default, svg on request, mermaid means "do not render".

`mermaid-render` and its theme file have shipped in this image for months and
the planning skill documents piping charts through it — but `mmdc` was never
installed, so every call ended at the wrapper's own guard and diagram output
silently degraded to ASCII. The renderer is now installed; this pins the part
that decides what comes out.

WHY THE FORMAT IS A SETTING AND NOT A FLAG
"Which format do charts use here" is answered once for a deployment, not
argued per call. So it is a file the bot writes, `--format` overrides it for a
single call, and png is what you get having said nothing.

`mermaid` is a valid SETTING but not a valid RENDER: it means post the source
block and let the destination draw it, which GitHub does. It must therefore
never produce a file — a `chart.mermaid` nobody can open is the failure this
guards.
"""

import os
import subprocess
import tempfile
import unittest

from harness import BUILDER

RENDER = os.path.join(BUILDER, "mermaid-render")


def render(args="", setting=None, source="graph TD\n  A-->B\n"):
    """Run the real wrapper with a stub `mmdc`, and return the output path.

    The stub records what it was asked to write and creates it. Installing a
    headless browser to assert a filename would be absurd, and stubbing the
    one external command keeps every line of the wrapper's own logic under
    test — the argument parsing, the setting, the extension.
    """
    with tempfile.TemporaryDirectory() as home:
        os.makedirs(os.path.join(home, ".openclaw"), exist_ok=True)
        if setting is not None:
            with open(os.path.join(home, ".openclaw", ".chart-format"), "w") as fh:
                fh.write(setting + "\n")
        bindir = os.path.join(home, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "mmdc")
        with open(stub, "w") as fh:
            fh.write(
                "#!/bin/bash\n"
                "out=''\n"
                "while [ $# -gt 0 ]; do\n"
                "  case \"$1\" in --output) out=\"$2\"; shift 2 ;; *) shift ;; esac\n"
                "done\n"
                "printf '<svg/>' > \"$out\"\n")
        os.chmod(stub, 0o755)
        src = os.path.join(home, "chart.mmd")
        with open(src, "w") as fh:
            fh.write(source)
        # INVOKED THROUGH `bash`, NOT EXECUTED.
        #
        # Every script in this repository is committed 100644 — the executable
        # bit is applied by the Dockerfile's `--chmod=0755`, not carried in
        # git. Running it directly works in the sandbox this is developed in,
        # because that mount forces 0755 on every file, and fails in CI with
        # "Permission denied" (exit 126). Same trap test_source_lib.py already
        # documents for the mode-based guard it deliberately does not use.
        r = subprocess.run(
            ["bash", "-c", f'export HOME="{home}" PATH="{bindir}:$PATH"; '
                           f'bash "{RENDER}" {args} "{src}"'],
            capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout.strip(), r.stderr.strip()


class TheDefaultIsPng(unittest.TestCase):
    def test_nothing_set_gives_png(self):
        rc, out, err = render()
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.endswith(".png"), out)

    def test_the_missing_setting_file_is_not_an_error(self):
        # The file is optional. Its absence must not fail the run or produce
        # an empty extension.
        rc, out, err = render(setting=None)
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.endswith(".png"), out)


class TheSettingIsHonoured(unittest.TestCase):
    def test_svg_setting(self):
        rc, out, err = render(setting="svg")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.endswith(".svg"), out)

    def test_the_flag_overrides_the_setting(self):
        # One call wanting something else must not require changing the
        # deployment-wide setting and changing it back.
        rc, out, err = render(args="--format png", setting="svg")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.endswith(".png"), out)


class MermaidNeverProducesAFile(unittest.TestCase):
    def test_the_mermaid_setting_falls_back_to_png(self):
        # `mermaid` means "post the source, do not render" and is handled by
        # the skill before the renderer is called. If it reaches here anyway,
        # a chart.mermaid nobody can open is worse than a png.
        rc, out, err = render(setting="mermaid")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.endswith(".png"), out)

    def test_any_other_value_falls_back_too(self):
        rc, out, err = render(setting="gif")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.endswith(".png"), out)


class TheImageInstallsTheRenderer(unittest.TestCase):
    """The bug this closes: the wrapper shipped, its driver did not."""

    def setUp(self):
        with open(os.path.join(BUILDER, "Dockerfile"), encoding="utf-8") as fh:
            self.docker = fh.read()

    def test_mermaid_cli_is_installed(self):
        self.assertIn("@mermaid-js/mermaid-cli@", self.docker)

    def test_the_version_is_pinned_in_versions(self):
        with open(os.path.join(os.path.dirname(BUILDER), "VERSIONS"),
                  encoding="utf-8") as fh:
            self.assertIn("MERMAID_CLI_VERSION=", fh.read())

    def test_puppeteer_reuses_the_system_chromium(self):
        # Otherwise puppeteer downloads a SECOND browser into an image that
        # already installs one.
        self.assertIn("PUPPETEER_SKIP_DOWNLOAD", self.docker)
        self.assertIn("PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium", self.docker)

    def test_the_build_proves_it_renders(self):
        # `command -v mmdc` only shows the package unpacked. The build renders
        # a real diagram, because the way this fails is at render time.
        self.assertIn("smoke.mmd", self.docker)


if __name__ == "__main__":
    unittest.main()

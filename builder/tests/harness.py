"""Shared test harness: import the builder modules, and fake what costs money.

TWO PROBLEMS THIS SOLVES
------------------------
1. The builder modules are installed FLAT into /usr/local/bin in the image and
   imported by bare name (`import issue_priority`). In the repo they sit in
   builder/. Tests must import them the same way the runtime does, or they
   test a different arrangement than the one that ships.

2. Several units are shell, not Python — agent-limits, agent-models,
   agent-slot — and the runner scripts shell out to `openclaw`, `curl`, `git`
   and `az`. Testing any of that for real would mean a model call, a network
   and an Azure login. `fake_path()` puts stand-ins first on PATH instead.

Deliberately dependency-free: unittest from the standard library, no pytest.
The image has no pip packages at all, and a test suite that only runs where
someone remembered to pip install is a test suite that stops being run.
"""

from __future__ import annotations

import contextlib
import importlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_counter = itertools.count()

# Test sandboxes live UNDER THE REPO, not in the system temp directory.
#
# Not a style choice: the shell half of this suite runs bash, and in the
# sandboxed environment this repo is developed in, bash cannot see
# %LOCALAPPDATA%\Temp at all — a script written there by Python is reported as
# "No such file or directory" in every path spelling. Everything under the
# checkout is visible to both. CI on Linux does not care either way.
#
# Override with CLAW_TEST_TMP if a checkout is ever on a read-only volume.
TMP_ROOT = os.environ.get(
    "CLAW_TEST_TMP",
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), ".test-tmp"))

BUILDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")


def bash_path(path: str) -> str:
    """A path spelled the way the bash on THIS machine reads it.

    `BUILDER` and the sandbox directories are native paths, and on Windows a
    native path is not something bash can open: it reads `C:\\...` as a
    relative name and the backslashes as escape characters. The failure is
    quiet in the worst way —

        /bin/bash: C:\\projects\\...\\project-kind.sh: No such file or directory
        /bin/bash: detect_project_annotations_from_dir: command not found

    — because the missing `.` source leaves the FUNCTION undefined, the
    caller's `$PROJECT_ANNOTATIONS` is simply empty, and the assertion fails
    reporting a wrong answer rather than a path that does not exist.

    The prefix is ASKED of bash rather than assumed, because which bash wins
    is not knowable from here. On a machine with both WSL and Git for Windows
    installed, `subprocess` resolves a bare `bash` through the system PATH and
    System32 comes first, so the tests run under WSL — which mounts the drive
    at /mnt/c and cannot see either `C:/...` or Git Bash's `/c/...`. Guessing
    a prefix gets one of the two wrong, and gets it wrong silently: the source
    fails, the functions are never defined, and the assertion reports an empty
    answer.

    So it is probed once: bash is asked for `pwd` in the repository root, and
    every path under the repository is expressed relative to that.

    On Linux there is no drive letter, the probe returns the path it was
    given, and this is the identity function.
    """
    p = os.path.abspath(path)
    if os.name != "nt":
        return p.replace("\\", "/")
    root_native, root_bash = _bash_root()
    if root_bash:
        try:
            rel = os.path.relpath(p, root_native).replace("\\", "/")
        except ValueError:      # different drive - no relative path exists
            rel = ""
        if rel and not rel.startswith(".."):
            return f"{root_bash}/{rel}"
    # Outside the repository, or the probe failed: the Git Bash spelling is
    # the better of the two guesses, since that is the shell a developer runs.
    q = p.replace("\\", "/")
    return "/" + q[0].lower() + q[2:] if len(q) > 1 and q[1] == ":" else q


_BASH_ROOT: tuple[str, str] | None = None


def _bash_root() -> tuple[str, str]:
    """(repo root as this OS spells it, repo root as bash spells it)."""
    global _BASH_ROOT
    if _BASH_ROOT is None:
        native = os.path.dirname(BUILDER)
        seen = ""
        try:
            out = subprocess.run(["bash", "-c", "pwd"], cwd=native,
                                 capture_output=True, text=True, timeout=30)
            if out.returncode == 0:
                seen = out.stdout.strip()
        except Exception:  # noqa: BLE001 - no bash at all is "cannot translate"
            seen = ""
        _BASH_ROOT = (native, seen)
    return _BASH_ROOT


def sandbox_root() -> str:
    """TMP_ROOT, created if it is not there yet.

    For the tests that build a throwaway tree at MODULE level rather than in
    `ShellTestCase.setUp`, which is where TMP_ROOT is otherwise created.

    They must not reach for `tempfile.TemporaryDirectory()` with no `dir`:
    that lands in the system temp directory, which — per the note on TMP_ROOT
    above — bash cannot see in the sandbox this repo is developed in. The
    symptom is not a missing directory but a shell unit that runs, finds
    nothing, and returns the empty string, so the assertion fails on a
    plausible-looking wrong ANSWER instead of on a broken path.
    """
    os.makedirs(TMP_ROOT, exist_ok=True)
    return TMP_ROOT

if BUILDER not in sys.path:
    sys.path.insert(0, BUILDER)


def load(name: str):
    """Import a builder module, fresh.

    Fresh matters: several modules read configuration from the environment at
    import time (paths, endpoints), so a test that changes the environment
    needs the module re-read rather than the copy an earlier test imported.
    """
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


def load_script(name: str):
    """Import a builder SCRIPT — a file with no .py extension.

    llm-quota, project-allow and the tick scripts are installed without an
    extension. importlib cannot find those by name, so load them by path.
    """
    import importlib.util
    path = os.path.join(BUILDER, name)
    spec = importlib.util.spec_from_loader(
        name.replace("-", "_"),
        importlib.machinery.SourceFileLoader(name.replace("-", "_"), path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ShellTestCase(unittest.TestCase):
    """Base for tests that run the shell units in a throwaway HOME.

    Every shell unit here stores state under $HOME/.openclaw, so an isolated
    HOME is what keeps one test from seeing another's leftovers — and keeps
    the suite from touching the developer's real workspace.
    """

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.home = tempfile.mkdtemp(prefix="clawtest-", dir=TMP_ROOT)
        os.makedirs(os.path.join(self.home, ".openclaw"), exist_ok=True)
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin, exist_ok=True)
        # Copy the units under test next to each other: agent-models sources
        # agent-models.sh from its own directory, exactly as in the image.
        for f in ("agent-limits", "agent-limits.sh",
                  "agent-models", "agent-models.sh", "agent-slot.sh",
                  "agent-thinking", "agent-thinking.sh",
                  "security-level", "mermaid-render", "telegram-notify",
                  "review-verdict"):
            src = os.path.join(BUILDER, f)
            if os.path.exists(src):
                _install(src, os.path.join(self.bin, f))
        for f in os.listdir(FAKES):
            _install(os.path.join(FAKES, f), os.path.join(self.bin, f))
        self.env = dict(os.environ)
        # PATH and HOME are consumed by BASH, not by the host OS, so they must
        # be POSIX even when the suite runs on Windows. Using os.pathsep here
        # produced a semicolon-joined PATH that bash read as one nonsense
        # entry: every CLI became unfindable and all 29 shell tests failed with
        # empty output, which reads exactly like a logic bug and was not one.
        self.env["HOME"] = _posix(self.home)
        self.env["PATH"] = _posix(self.bin) + ":" + _posix_path_list(
            os.environ.get("PATH", ""))
        # Relative on purpose: the fake writes it from inside the shell, whose
        # $PWD is the sandbox. An absolute host path is not resolvable there.
        self.calls = os.path.join(self.home, "openclaw-calls.jsonl")
        self.env["FAKE_OPENCLAW_CALLS"] = "$PWD/openclaw-calls.jsonl"

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def sh(self, script: str, **env):
        """Run a shell snippet with the fakes on PATH. Returns (rc, out, err).

        HOME and PATH are exported INSIDE the shell rather than passed through
        the environment. Under MSYS (Git Bash on Windows) the startup code
        re-derives both from the Windows environment, so an inherited HOME was
        silently replaced by /home/<user> and the inherited PATH was discarded
        — every CLI became unfindable and the whole shell suite failed with
        empty output. Exporting in-shell is immune to that and behaves
        identically on Linux.
        """
        e = dict(self.env)
        e.update({k: str(v) for k, v in env.items()})
        e.setdefault("MSYS_NO_PATHCONV", "1")
        e.setdefault("MSYS2_ARG_CONV_EXCL", "*")
        # Everything is RELATIVE to cwd, and the shell derives its own absolute
        # paths from $PWD.
        #
        # The reason is blunt: the bash this suite runs under does not resolve
        # absolute host paths at all. A script at C:/projects/... is "No such
        # file or directory" in every spelling — native, drive-plus-slash and
        # /c/... alike — while the same file opened relative to the working
        # directory runs fine. So the script name goes in relative, and HOME
        # and PATH are built from $PWD inside the shell, where they are
        # guaranteed to be spelled the way that shell understands. On Linux
        # this is unremarkable and identical.
        name = f"_run_{next(_counter)}.sh"
        # Per-call variables are exported IN the script for the same reason as
        # HOME and PATH: the process environment does not survive the hop into
        # this shell reliably, and a FAKE_OPENCLAW_RC that silently fails to
        # arrive turns a test of the failure path into a test of the happy one
        # that passes for the wrong reason.
        # The prefixes this codebase configures its units with. FAKE_ alone was
        # too narrow and failed silently in exactly the way this comment warns
        # about: a test passing MERMAID_THEME_FILE saw it dropped, the renderer
        # took its "no theme file" fallback, and the assertion measured the
        # fallback while claiming to measure the theme.
        _PASS_THROUGH = ("FAKE_", "MERMAID_", "PLANNING_", "AGENT_", "SPRINT_",
                         "CLAW_")
        exports = "\n".join(
            f"export {k}=\"{v}\"" for k, v in sorted(e.items())
            if k.startswith(_PASS_THROUGH))
        body = "\n".join([
            'export HOME="$PWD"',
            'export PATH="$PWD/bin:$PATH"',
            exports,
            script,
            "",
        ])
        with open(os.path.join(self.home, name), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(body)
        p = subprocess.run(["bash", name], capture_output=True,
                           text=True, env=e, cwd=self.home, timeout=120)
        return (p.returncode, p.stdout, p.stderr)

    def run_unit(self, argv: list[str], **env):
        """Run one of the copied CLIs by name."""
        return self.sh(" ".join(_q(a) for a in argv), **env)

    def openclaw_calls(self) -> list[dict]:
        """Every fake `openclaw` invocation so far, oldest first."""
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def extract_block(self, script: str, start: str, end: str) -> str:
        """Pull a self-contained block out of a runner and write it to a file.

        The runner scripts are 2000 lines and demand an API token, a repo and
        a pod before they will do anything. The DECISIONS worth testing —
        which branch to check out, whether to wake the agent — sit in blocks
        that only need a few variables. Extracting the real lines (never a
        copy) keeps the test honest: it exercises what ships, and it fails
        when someone edits the runner.
        """
        path = os.path.join(BUILDER, script)
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        try:
            i = next(n for n, l in enumerate(lines) if l.startswith(start))
            j = next(n for n, l in enumerate(lines[i:], i) if l.startswith(end))
        except StopIteration:  # pragma: no cover - a rename should fail loudly
            raise AssertionError(
                f"could not find the block {start!r}..{end!r} in {script}. "
                "If the runner was restructured, update the test rather than "
                "deleting it — this block is the one that discarded work.")
        out = os.path.join(self.home, "block.sh")
        # newline="\n" is load-bearing: without it Python writes CRLF on
        # Windows and bash then fails on every line, silently enough that the
        # test just sees "the block produced no output" and reads like a logic
        # failure.
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines[i:j]) + "\n")
        return out


def fake_path(into: str) -> str:
    """Copy the stand-ins into `into` and return it, ready to prepend to PATH.

    ALWAYS COPY, NEVER POINT PATH AT THE FAKES DIRECTORY IN THE CHECKOUT.

    A fake is only a fake if the shell can execute it, and whether the file in
    the checkout carries its execute bit is a property of git and of whoever
    cloned it — not something a test should depend on. That bit was recorded
    as 0644 once: every one of these files came out of a fresh clone
    non-executable, so on a machine without the real binary installed the
    lookup found NOTHING, the code under test took its "the tool is not
    available here" branch, and three tests failed while the rest of the suite
    passed for reasons that had nothing to do with the fakes.

    `_install` sets the bit on the copy, so the sandbox is correct however the
    checkout arrived.
    """
    os.makedirs(into, exist_ok=True)
    for name in os.listdir(FAKES):
        _install(os.path.join(FAKES, name), os.path.join(into, name))
    return into


def _install(src: str, dst: str) -> None:
    """Copy a script into the sandbox with LF endings and the execute bit.

    The checkout on Windows has CRLF, which makes the shebang end in a
    carriage return — the kernel then looks for an interpreter called
    "/bin/sh\\r" and reports the far-from-obvious "required file not found",
    naming the SCRIPT rather than the interpreter. Every shell unit failed
    that way until the endings were normalised here.

    This is a property of the developer's checkout, not of the image: the
    Docker build copies from a Linux context. Normalising on the way into the
    sandbox keeps the suite honest on both.
    """
    with open(src, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")
    with open(dst, "wb") as f:
        f.write(data)
    os.chmod(dst, 0o755)


def _q(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def _posix(path: str) -> str:
    """A Windows path as bash sees it: C:\\x\\y -> /c/x/y.

    The suite is developed on Windows against Git Bash and runs on Linux in
    CI. Paths handed to bash have to be POSIX in both places; paths handed to
    subprocess.run (cwd) must stay native.
    """
    if os.name != "nt":
        return path
    p = path.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


def _posix_path_list(value: str) -> str:
    """Translate a PATH list to the separator and spelling bash expects."""
    if os.name != "nt":
        return value
    # A Git Bash PATH is already colon-separated and POSIX-spelled; a native
    # Windows one is not. Handle whichever we were handed.
    parts = value.split(";") if ";" in value else value.split(":")
    return ":".join(_posix(p) for p in parts if p)


@contextlib.contextmanager
def temp_env(**kw):
    """Set environment variables for the duration of a block."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

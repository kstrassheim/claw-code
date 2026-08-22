"""Every in-house MCP server must DECLARE what it imports.

An MCP server that imports a package it does not declare installs cleanly,
ships, and then dies at startup with `ERR_MODULE_NOT_FOUND`. openclaw reports
that as `failed to start server "<name>"` and carries on without the tools —
so the agent simply cannot do a thing it was told it could, and nothing fails
loudly enough to notice.

Six of these servers shipped that way. It went unseen because only one server
was registered in `mcp.servers`; the broken ones were never started, so the
defect was invisible until they were wired in.

This checks the property directly — imports are a subset of declarations —
rather than checking for one package by name, so the next undeclared import
is caught too.
"""

import json
import os
import re
import unittest

from harness import BUILDER

# A directory is an in-house MCP server if it holds a package.json and a .mjs.
def servers():
    for name in sorted(os.listdir(BUILDER)):
        d = os.path.join(BUILDER, name)
        if not os.path.isdir(d) or not name.endswith("-mcp"):
            continue
        if os.path.exists(os.path.join(d, "package.json")):
            yield name, d


# Bare specifiers only: relative paths and node: builtins are not dependencies.
IMPORT = re.compile(r"""(?:^|\s)(?:import|export)[^'"]*?from\s*['"]([^'"]+)['"]""", re.M)
REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")


def package_of(spec: str) -> str | None:
    """The npm package a specifier resolves to, or None if it is not one."""
    if spec.startswith((".", "/", "node:", "#")):
        return None
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


class EveryImportIsDeclared(unittest.TestCase):
    def test_there_are_servers_to_check(self):
        # A rename that empties the discovery would make every test below pass
        # while checking nothing.
        self.assertGreaterEqual(len(list(servers())), 6)

    def test_every_imported_package_is_declared(self):
        for name, d in servers():
            with self.subTest(server=name):
                declared = set(json.load(
                    open(os.path.join(d, "package.json"), encoding="utf-8")
                ).get("dependencies", {}))
                imported = set()
                for f in os.listdir(d):
                    if not f.endswith(".mjs"):
                        continue
                    src = open(os.path.join(d, f), encoding="utf-8").read()
                    for spec in IMPORT.findall(src) + REQUIRE.findall(src):
                        pkg = package_of(spec)
                        if pkg:
                            imported.add(pkg)
                missing = sorted(imported - declared)
                self.assertEqual(
                    missing, [],
                    f"{name} imports {missing} without declaring it — "
                    f"npm install will not fetch it and the server dies at "
                    f"startup with ERR_MODULE_NOT_FOUND, which openclaw "
                    f"reports only as 'failed to start server'")

    def test_the_sdk_version_is_consistent(self):
        # Divergent SDK majors across servers is the next quiet failure: one
        # server starts, another does not, for reasons that look unrelated.
        versions = {}
        for name, d in servers():
            deps = json.load(
                open(os.path.join(d, "package.json"), encoding="utf-8")
            ).get("dependencies", {})
            v = deps.get("@modelcontextprotocol/sdk")
            if v:
                versions.setdefault(v, []).append(name)
        self.assertLessEqual(
            len(versions), 1,
            f"in-house MCP servers pin different SDK versions: {versions}")


class RegisteredServersExist(unittest.TestCase):
    """Anything wired into mcp.servers must actually be installed."""

    def test_every_registered_in_house_server_is_built(self):
        # The embedded JSON is pulled out by hand rather than with a YAML
        # parser. This suite runs with the standard library only — the same
        # constraint the runtime image has — and a test that needs a pip
        # install is a test that stops running.
        cfg_path = os.path.join(os.path.dirname(BUILDER), "k8s",
                                "010-openclaw-config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        start = next(i for i, l in enumerate(lines)
                     if l.strip().startswith("openclaw.json:"))
        indent = len(lines[start + 1]) - len(lines[start + 1].lstrip())
        block = []
        for line in lines[start + 1:]:
            if line.strip() and (len(line) - len(line.lstrip())) < indent:
                break
            block.append(line[indent:])
        cfg = json.loads("\n".join(block))
        for name, spec in cfg.get("mcp", {}).get("servers", {}).items():
            args = " ".join(spec.get("args", []))
            m = re.search(r"/opt/([a-z0-9-]+)/[a-z0-9-]+\.mjs", args)
            if not m:
                continue          # not one of ours (a binary on PATH)
            with self.subTest(server=name):
                src = os.path.join(BUILDER, m.group(1))
                self.assertTrue(
                    os.path.isdir(src),
                    f"mcp.servers.{name} points at /opt/{m.group(1)}, which "
                    f"this repository does not build")


if __name__ == "__main__":
    unittest.main()

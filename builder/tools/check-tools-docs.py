#!/usr/bin/env python3
"""Every tools document must actually reach the agent.

TWO SILENT FAILURES, ONE CHECK

1. THE CONCATENATION IS NOT A GLOB.
   The PreSync hook assembles the agent's ambient TOOLS.md by `cat`-ing a
   hardcoded list of files. A new builder/tools-md/*.md that nobody adds to ORDER
   is written, reviewed, merged and then never loaded — and nothing anywhere
   says so. The document simply has no effect, which reads as "the agent
   ignores its instructions" rather than as a missing line in a shell script.

2. THE RESULT IS TRUNCATED WITHOUT WARNING.
   openclaw caps how much bootstrap context it will read. When the assembled
   file exceeds `bootstrapMaxChars` the tail is dropped silently: the agent
   keeps working, with no error and no log line, having simply never seen the
   last N kilobytes of its own tool documentation. That is how a tester ends
   up stranded at a login screen it has documented instructions for.

   This is not hypothetical. The cap sat at 30000 while the assembled file was
   109023 characters, so roughly seven of every ten characters of tool
   documentation were being discarded on every single turn.

Run:  python builder/tools/check-tools-docs.py
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOLS_DIR = os.path.join(ROOT, "builder", "tools-md")
# The concatenation order, which used to live inside the ArgoCD pre-sync hook.
# Moving it beside the documents is what lets the image build and render-config
# assemble the same file — the hook was the only copy, and only the cluster
# ever read it.
ORDER = os.path.join(TOOLS_DIR, "ORDER")
CONFIG = os.path.join(ROOT, "k8s", "010-openclaw-config.yaml")

# Headroom over the current size. A cap set to exactly today's byte count
# passes today and truncates on the next paragraph anyone writes.
HEADROOM = 1.25


def fail(msg: str) -> None:
    print(f"  FAIL: {msg}", file=sys.stderr)
    globals()["FAILURES"] += 1


FAILURES = 0


def main() -> int:
    on_disk = sorted(
        os.path.basename(p) for p in glob.glob(os.path.join(TOOLS_DIR, "*.md")))
    if not on_disk:
        print(f"ERROR: no documents found in {TOOLS_DIR}", file=sys.stderr)
        return 2

    with open(ORDER, encoding="utf-8") as f:
        listed = [ln.strip() for ln in f
                  if ln.strip() and not ln.lstrip().startswith("#")]

    print("== every tools document is assembled ==")
    missing = [n for n in on_disk if n not in listed]
    for name in missing:
        fail(f"builder/tools-md/{name} exists but ORDER never names it — "
             f"it will be merged and then silently never loaded")
    stale = [n for n in listed if n not in on_disk]
    for name in stale:
        fail(f"ORDER names builder/tools-md/{name}, which does not exist — "
             f"the image build fails, and so does assembly at pod start")
    if not missing and not stale:
        print(f"  ok:   all {len(on_disk)} documents are in the assembly list")

    print("== the assembled document fits in the bootstrap budget ==")
    total = 0
    for name in listed:
        path = os.path.join(TOOLS_DIR, name)
        if os.path.exists(path):
            total += os.path.getsize(path)

    with open(CONFIG, encoding="utf-8") as f:
        config_text = f.read()
    caps = {}
    for key in ("bootstrapMaxChars", "bootstrapTotalMaxChars"):
        m = re.search(rf'"{key}"\s*:\s*(\d+)', config_text)
        if m:
            caps[key] = int(m.group(1))
        else:
            fail(f"{key} is not set in k8s/010-openclaw-config.yaml — "
                 f"the default is far below what this repo ships")

    per_file = caps.get("bootstrapMaxChars")
    if per_file is not None:
        needed = int(total * HEADROOM)
        if per_file < total:
            fail(f"bootstrapMaxChars is {per_file} but the assembled TOOLS.md "
                 f"is {total} characters. {total - per_file} characters are "
                 f"being dropped from every turn, silently. Raise it to at "
                 f"least {needed}.")
        elif per_file < needed:
            fail(f"bootstrapMaxChars is {per_file}, only {per_file - total} "
                 f"characters above the current {total}. Raise it to at least "
                 f"{needed} so the next paragraph does not truncate the file.")
        else:
            print(f"  ok:   assembled {total} chars, cap {per_file} "
                  f"({per_file - total} spare)")

    total_cap = caps.get("bootstrapTotalMaxChars")
    if total_cap is not None and per_file is not None and total_cap < per_file:
        fail(f"bootstrapTotalMaxChars ({total_cap}) is below bootstrapMaxChars "
             f"({per_file}), so the per-file cap can never be reached")

    if FAILURES:
        print(f"\nFAILED: {FAILURES} problem(s). A tools document that does "
              f"not reach the agent is not documentation.", file=sys.stderr)
        return 1
    print("\nPASS: every tools document is assembled and fits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

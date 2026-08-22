#!/usr/bin/env python3
"""Every callable that ships on the openclaw instance, and whether a test names it.

WHY A LEDGER AND NOT A COVERAGE TOOL
Half of what runs on the bot is shell. `coverage.py` cannot see a bash
function, and nothing in the image can instrument one. So this does the honest
version: enumerate every function that ships, and cross-reference the test
suite for a mention of its name.

A NAME MENTIONED IN A TEST IS NOT PROOF OF COVERAGE. It is the weakest useful
signal, and it is deliberately weak — the point is to make the UNTESTED list
visible and to stop it growing silently, not to produce a percentage anyone
can wave around. Read "tested" here as "somebody at least thought about it".

Usage:
    python builder/tools/function-inventory.py            # summary
    python builder/tools/function-inventory.py --untested # just the gaps
    python builder/tools/function-inventory.py --check    # fail on regression
    python builder/tools/function-inventory.py --baseline # rewrite the ledger

--check is what CI runs: it fails if a NEW untested function appears, so the
gap can shrink but not grow. The baseline is committed alongside.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILDER = os.path.dirname(HERE)
TESTS = os.path.join(BUILDER, "tests")
BASELINE = os.path.join(HERE, "function-inventory.json")

# Shell function definitions: `name() {` and `function name {`.
SH_DEF = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")
PY_DEF = re.compile(r"^(?:\s*)def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Files that are not "the bot": the test suite itself, and these tools.
SKIP_DIRS = {"tests", "tools"}

# Private helpers with a leading underscore are implementation detail; they are
# still listed, but a test naming the public function that calls them counts.
def is_private(name: str) -> bool:
    return name.startswith("_")


def shipped_files() -> list[str]:
    out = []
    for entry in sorted(os.listdir(BUILDER)):
        path = os.path.join(BUILDER, entry)
        if os.path.isdir(path):
            continue
        if entry.endswith((".md", ".json", ".txt", ".env")):
            continue
        out.append(path)
    return out


def functions_in(path: str) -> list[str]:
    names = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = SH_DEF.match(line) or PY_DEF.match(line)
                if m:
                    names.append(m.group(1))
    except OSError:
        pass
    # Stable and deduplicated: a function defined twice is one thing to test.
    return sorted(set(names))


def test_corpus() -> str:
    body = []
    for root, dirs, files in os.walk(TESTS):
        dirs[:] = [d for d in dirs if d != "fixtures"]
        for name in files:
            if name.endswith((".py", ".sh")) or "." not in name:
                try:
                    with open(os.path.join(root, name), encoding="utf-8",
                              errors="replace") as f:
                        body.append(f.read())
                except OSError:
                    pass
    return "\n".join(body)


def inventory() -> dict[str, dict[str, list[str]]]:
    corpus = test_corpus()
    result: dict[str, dict[str, list[str]]] = {}
    for path in shipped_files():
        rel = os.path.relpath(path, BUILDER)
        names = functions_in(path)
        if not names:
            continue
        tested, untested = [], []
        for n in names:
            # Word-boundary match so `parse` does not count as covering
            # `parse_something`.
            if re.search(rf"\b{re.escape(n)}\b", corpus):
                tested.append(n)
            else:
                untested.append(n)
        result[rel] = {"tested": tested, "untested": untested}
    return result


def summarise(inv: dict) -> tuple[int, int]:
    t = sum(len(v["tested"]) for v in inv.values())
    u = sum(len(v["untested"]) for v in inv.values())
    return t, u


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--untested", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    args = ap.parse_args(argv)

    inv = inventory()
    tested, untested = summarise(inv)
    total = tested + untested

    if args.baseline:
        flat = sorted(f"{f}:{n}" for f, v in inv.items() for n in v["untested"])
        with open(BASELINE, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"untested": flat}, f, indent=2)
            f.write("\n")
        print(f"baseline written: {len(flat)} untested function(s)")
        return 0

    if args.check:
        try:
            with open(BASELINE, encoding="utf-8") as f:
                known = set(json.load(f)["untested"])
        except (OSError, ValueError):
            print("no baseline — run with --baseline first", file=sys.stderr)
            return 1
        now = {f"{f}:{n}" for f, v in inv.items() for n in v["untested"]}
        new = sorted(now - known)
        if new:
            print("NEW untested function(s) — add a test or update the "
                  "baseline deliberately:", file=sys.stderr)
            for n in new:
                print(f"  {n}", file=sys.stderr)
            return 1
        gone = sorted(known - now)
        print(f"no new untested functions ({len(now)} known gaps"
              + (f", {len(gone)} closed since the baseline)" if gone else ")"))
        for n in gone:
            print(f"  closed: {n}")
        return 0

    if args.untested:
        for f in sorted(inv):
            if inv[f]["untested"]:
                print(f"{f}")
                for n in inv[f]["untested"]:
                    print(f"    {n}")
        return 0

    print(f"{'FILE':<34} {'FUNCS':>6} {'TESTED':>7} {'GAP':>5}")
    for f in sorted(inv, key=lambda k: -len(inv[k]["untested"])):
        v = inv[f]
        n = len(v["tested"]) + len(v["untested"])
        print(f"{f:<34} {n:>6} {len(v['tested']):>7} {len(v['untested']):>5}")
    pct = (100 * tested / total) if total else 0
    print(f"\n{total} functions ship, {tested} are named by a test "
          f"({pct:.0f}%), {untested} are not")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

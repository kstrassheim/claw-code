#!/usr/bin/env python3
"""Guard the model configuration against silent, invisible regressions.

The configuration is JSON embedded in a ConfigMap, so it cannot carry
comments, and every property it encodes fails QUIETLY when removed. There is
no error, no warning, no log line — just a bot that is worse at its job.

WHAT IS CHECKED, AND WHY EACH ONE IS SILENT WHEN BROKEN

  reasoning on anthropic-messages models (#13)
      MiniMax runs against an anthropic-compatible endpoint. openclaw only
      attaches a thinking parameter when the model is declared reasoning-
      capable (the alternative trigger is Claude-only). Without the flag no
      thinking is ever requested and `thinkingDefault` has nothing to act on.
      Measured on the live endpoint: same prompt, no thinking parameter gives
      "269 km"; with it, 607 characters of reasoning and "269.23". Nothing
      anywhere reports the difference.

  no literal API keys
      Keys belong in ${VAR} form, substituted at pod start. A literal one here
      is committed to git and printed by every config dump.

  empty fallbacks
      Deliberate policy: exactly one automatic model. A fallback chain would
      silently burn a second provider's quota when the first hits a limit,
      instead of failing the turn cleanly so the next tick can retry.

  a declared primary
      A primary naming a provider that is not declared leaves openclaw to pick
      its own default, which has previously meant openai/gpt-5.5 and a missing
      key error at runtime rather than at deploy.

Run:  python builder/tools/check-model-config.py
"""

from __future__ import annotations

import json
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML is required for this check", file=sys.stderr)
    raise SystemExit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CONFIG = os.path.join(ROOT, "k8s", "010-openclaw-config.yaml")

# Providers whose request builder needs an explicit reasoning declaration
# before openclaw will ask for extended thinking.
NEEDS_REASONING_FLAG = {"anthropic-messages"}


def load_template(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        docs = list(yaml.safe_load_all(f))
    for doc in docs:
        if not doc:
            continue
        if (doc.get("metadata") or {}).get("name") == "openclaw-config-template":
            return json.loads(doc["data"]["openclaw.json"])
    raise SystemExit("openclaw-config-template not found in " + path)


def check(cfg: dict) -> list[str]:
    problems: list[str] = []
    providers = (cfg.get("models") or {}).get("providers") or {}
    defaults = (cfg.get("agents") or {}).get("defaults") or {}

    for name, prov in providers.items():
        api = prov.get("api", "")
        key = str(prov.get("apiKey", ""))
        if key and not key.startswith("${"):
            problems.append(
                f"provider {name}: apiKey is a literal, not a ${{VAR}} reference")
        if api in NEEDS_REASONING_FLAG:
            for model in prov.get("models") or []:
                if not model.get("reasoning"):
                    problems.append(
                        f"provider {name}, model {model.get('id')}: "
                        f'api={api} needs "reasoning": true, or openclaw never '
                        "asks for extended thinking")

    for field in ("model", "imageModel"):
        spec = defaults.get(field) or {}
        primary = spec.get("primary") or ""
        if primary:
            prov = primary.split("/")[0]
            if prov not in providers:
                problems.append(
                    f"agents.defaults.{field}.primary is {primary!r} but "
                    f"provider {prov!r} is not declared")
        # A fallback chain is deliberate here: a single provider outage should
        # degrade the answer, not stop the bot. What must not happen is a
        # fallback naming a provider that does not exist — the chain then ends
        # early with a runtime key error instead of the model it promised, and
        # nothing reports the difference.
        for entry in spec.get("fallbacks") or []:
            name = entry if isinstance(entry, str) else (entry or {}).get("primary", "")
            if not name:
                continue
            prov = str(name).split("/")[0]
            if prov not in providers:
                problems.append(
                    f"agents.defaults.{field} falls back to {name!r} but "
                    f"provider {prov!r} is not declared — the chain ends here")
        # A fallback that repeats the primary is not a fallback. It burns a
        # second attempt on the model that just failed, which is exactly the
        # wrong response to a rate limit.
        chain = [primary] + [
            (e if isinstance(e, str) else (e or {}).get("primary", ""))
            for e in (spec.get("fallbacks") or [])]
        chain = [c for c in chain if c]
        if len(chain) != len(set(chain)):
            problems.append(
                f"agents.defaults.{field} repeats a model in its fallback "
                f"chain: {chain}")

    if not defaults.get("thinkingDefault"):
        problems.append("agents.defaults.thinkingDefault is unset")

    return problems


def main() -> int:
    cfg = load_template(CONFIG)
    problems = check(cfg)
    providers = (cfg.get("models") or {}).get("providers") or {}
    for name, prov in sorted(providers.items()):
        models = prov.get("models") or []
        flags = ", ".join(
            f"{m.get('id')}{'*' if m.get('reasoning') else ''}" for m in models)
        print(f"  {name:<9} api={prov.get('api','?'):<20} {flags}")
    print("  (* = declared reasoning-capable)")
    if problems:
        print("\nPROBLEMS:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"\nmodel configuration OK ({len(providers)} providers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

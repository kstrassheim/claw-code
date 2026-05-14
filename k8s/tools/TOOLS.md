<!--
  Managed by the claw-code k8s manifests. Source of truth:
  k8s/tools/*.md in the claw-code repo. Concatenated by the deploy
  workflow into the openclaw-tools-md ConfigMap and mounted read-only
  at ~/.openclaw/workspace/TOOLS.md (subPath file mount in
  k8s/020-deployment.yaml). Edits to this file inside the pod will
  fail with EROFS — change k8s/tools/ in the repo instead.
-->

# claw-code — coding agent capabilities

This file (and the per-tool sections appended below) is the bot's
self-description: when you don't know whether a capability is
wired, look here first. Anything **not** mentioned here is **not
wired** — surface that to the user instead of inventing tool
names or pretending you can act.

## What this image bundles

The container image (built from `builder/Dockerfile`) ships:

- **`git`** + **`gh`** (GitHub CLI) + the official
  `github-mcp-server` MCP — see TOOLS-github.md.
- **`kubectl`** + an in-house `mcp.servers.k8s` wrapper — see
  TOOLS-k8s.md.
- **`terraform`** + HashiCorp's `terraform-mcp-server` — see
  TOOLS-terraform.md.
- **`aws`** / **`gcloud`** / **`aliyun`** CLIs + matching
  `mcp.servers.aws|gcp|alicloud` wrappers — see TOOLS-aws.md,
  TOOLS-gcp.md, TOOLS-alicloud.md.
- **`az`** (Azure CLI) + a TOTP helper (`entra-totp`) for
  programmatic MFA — see TOOLS-entra.md.
- **`code-server`** (VSCode in the browser, bot-only loopback) —
  see TOOLS-vscode.md.
- **`mcp.servers.debug`** (Node V8-inspector / CDP) — see
  TOOLS-debug.md.
- **`podman`** (rootless, vfs storage) + **chromium** for
  in-pod container builds and browser-plugin automation.

GitLab support (`glab` CLI + `@yoda.digital/gitlab-mcp-server`) is
**documented** in TOOLS-gitlab.md but is **not** baked into this
image yet — the doc describes the target shape so the operator can
wire it.

## Provider-agnostic rules

Git/PR workflow rules — the mantra (1 issue = 1 branch = 1 PR/MR),
the ABSOLUTE rule against bypassing CI / branch protection, and the
read-and-react (👍) protocol — live in TOOLS-gitflow.md and apply
to **every** git host you push to. Read that first; the host-specific
docs only translate those rules into concrete tool calls.

## What is deliberately NOT in this image

- No Gmail / IMAP integration (use a generic SMTP MCP if you need
  email).
- No Vedic astrology / `jhora` / `pyswisseph` stack — claw-code is a
  coding agent, the upstream openclaw image strips those packages
  here.
- No ollama / local LLM runtime — chat models are configured via
  `openclaw.json` (Mistral + optional MiniMax by default).
- No Argo CD CLI — Helm/kubectl is the deploy surface; if you want
  Argo CD reach, the operator must wire it themselves.

If the user asks for one of the above, tell them it's not in the
image and offer the closest available alternative from the list
above.

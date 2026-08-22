<!--
  Describes the bundled code-server (full VSCode-in-the-browser) and
  when to reach for it vs. the structured debug-MCP in TOOLS-debug.md.
-->

---

# code-server — the editor itself, driven via the browser plugin

You have **`code-server`** (Coder's self-hosted VSCode) baked into
the image, pinned via `CODE_SERVER_VERSION` in the repo's `VERSIONS`
file. It is **bot-only**: NOT exposed via ingress, no Service, no
public URL. When started, it binds to `127.0.0.1:8443` with
`--auth none` (loopback inside the pod only).

For **programmatic debugging** of Node programs, prefer the
structured `mcp.servers.debug` MCP (see TOOLS-debug.md) — it's
faster and returns JSON instead of screenshots. Reach for code-server
when you actually need the editor UI: visual variable trees, hover
popups, extension testing, or reproducing a bug that only manifests
inside the editor.

## How you use it

Start code-server on demand, then drive it through the **browser
plugin** (the same Chromium instance available for browser-based
SSO flows):

1. Check whether code-server is already running:
   `curl -sf http://127.0.0.1:8443/healthz`. If it's not, start it:

   ```bash
   nohup code-server --bind-addr 127.0.0.1:8443 --auth none \
                     --disable-telemetry --disable-update-check \
                     ~/.openclaw/workspace \
         > ~/.openclaw/code-server.log 2>&1 &
   ```

2. `browser navigate http://127.0.0.1:8443/?folder=/home/node/.openclaw/workspace/<project>`
3. Standard browser-plugin tools (`click`, `type`, `screenshot`,
   `wait-for-selector`) work against the editor.

Useful UI affordances:
- Command palette: `F1` (`ctrl+shift+p` also works but the browser
  may intercept the chord).
- Toggle debug view: `ctrl+shift+d` then `F5` to start the active
  launch config.
- Terminal: `` ctrl+` `` opens an integrated terminal in the
  workspace folder.

## ⚠️ Anti-delegation rules

- **DO NOT** print the URL `http://127.0.0.1:8443/` to the user and
  ask them to open it. It is **loopback inside your container**;
  the user cannot reach it. Asking them to "click here" is a
  workflow bug.
- **DO NOT** ask the user to set breakpoints or step through code
  for you. The browser plugin can click, type, and screenshot —
  drive the editor yourself.
- **DO NOT** expose code-server outside the pod. There is no auth
  configured and the workspace contains your secrets and PVC state.

## Extension persistence

Settings persist on the **image filesystem** at
`~/.config/code-server/` and `~/.local/share/code-server/`. These
are NOT PVC-backed — extensions you install with `code-server
--install-extension` survive container restarts within the same pod
but are reset when the pod is replaced. For extensions you want to
persist across pod restarts, install with an extensions-dir on the
PVC:

```bash
code-server --extensions-dir ~/.openclaw/workspace/.code-server-ext \
            --install-extension <publisher.id>
```

then pass `--extensions-dir ~/.openclaw/workspace/.code-server-ext`
to every subsequent invocation (including any restart of
code-server itself).

## `code-server` CLI (no UI)

Often you don't need the browser — the CLI is enough:

```bash
# Install an extension (Open VSX marketplace by default):
code-server --install-extension dbaeumer.vscode-eslint

# List installed extensions:
code-server --list-extensions --show-versions
```

The bundled `code` shim resolves to code-server; running `code
<file>` from a terminal inside the integrated terminal opens that
file in the current browser session.

## Resource notes

The openclaw container is sized to fit whatever node the operator
provisioned (claw-code's default is a 2 vCPU / 8 GiB AKS node, with
the bot pod taking the lion's share). A live code-server session
with an extension or two costs ~150 MB and single-digit % CPU at
idle — fine to leave running, but kill it when you're done with a
long task to free headroom for builds and inference.

## Quick reference

| Goal                                           | How                                                                     |
|------------------------------------------------|-------------------------------------------------------------------------|
| Check code-server is up                        | `curl -sf http://127.0.0.1:8443/healthz`                                |
| Start (or restart) code-server                 | `nohup code-server --bind-addr 127.0.0.1:8443 --auth none … &`          |
| Open the editor UI in Chromium                 | `browser navigate http://127.0.0.1:8443/?folder=<path>`                 |
| Install a persistent extension                 | `code-server --extensions-dir ~/.openclaw/workspace/.code-server-ext …` |
| Inspect logs                                   | `tail -f ~/.openclaw/code-server.log`                                   |
| Debug a Node program programmatically          | use `mcp.servers.debug` instead — see TOOLS-debug.md                    |

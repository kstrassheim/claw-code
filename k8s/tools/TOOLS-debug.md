<!--
  Describes the in-house debug-MCP that drives the V8 inspector (CDP)
  for Node programs — the bot's "fix / verify a debugger config"
  workflow without scripting Chromium.
-->

---

# Debugger — `mcp.servers.debug` (Node CDP)

You have a custom **debug-MCP** wired in as `mcp.servers.debug`. It
spawns a Node program under V8's inspector and lets you drive it
with structured tool calls — set breakpoints, step, evaluate, look
at locals. No VSCode/code-server dependency: it talks raw CDP via
`chrome-remote-interface`, so it works even when no editor session
is connected.

This is the **preferred** way for you to debug Node programs in a
sandbox or to verify a `.vscode/launch.json` you're handing back
to a human developer. The code-server browser fallback (see
TOOLS-vscode.md) is still available for UI-only tasks (visual
variable inspection, hover popups) but is heavier.

**Scope:** Node only in this version. Python (debugpy/DAP) is on
the roadmap; for now, use `python -m pdb` from a shell if you need
Python debugging.

## Tool surface (all under `mcp.servers.debug.*`)

| Tool                          | Purpose                                                                 |
|-------------------------------|-------------------------------------------------------------------------|
| `debug_session_start`         | Spawn a Node program under `--inspect-brk`, attach, return session id.  |
| `debug_session_list`          | List active sessions.                                                   |
| `debug_session_state`         | Show running/paused/terminated + the most recent pause snapshot.        |
| `debug_session_resume`        | continue / step_over / step_into / step_out, waits for the next pause.  |
| `debug_session_set_breakpoint`| Breakpoint by absolute file + 1-indexed line; optional JS condition.    |
| `debug_session_remove_breakpoint` | Remove by id returned from set_breakpoint.                          |
| `debug_session_list_breakpoints` | List active breakpoints for a session.                               |
| `debug_session_evaluate`      | Evaluate a JS expression, optionally in a specific call frame.          |
| `debug_session_get_scope_variables` | Resolve a scope `objectId` (from `pauseDetail.callFrames[i].scopeChain[j].objectId`) to its variables — cleaner than crafting eval expressions for every local. |
| `debug_session_consume_output`| Drain the inferior's stdout/stderr accumulated since the last consume.  |
| `debug_session_stop`          | Kill the inferior and close the CDP connection. Idempotent.             |

## Typical workflow

```
# 1. Spawn the program. stop_on_entry pauses on the first line so you
#    can set breakpoints before any user code runs.
sid = debug_session_start(
  language = "node",
  program  = "/home/node/.openclaw/workspace/myproj/server.js",
  cwd      = "/home/node/.openclaw/workspace/myproj",
  args     = ["--port", "0"],
  env      = { "NODE_ENV": "test" },
  stop_on_entry = True,
)

# 2. Set a breakpoint at server.js:42, conditional on req.url containing "/health".
bp = debug_session_set_breakpoint(
  session_id = sid,
  file       = "/home/node/.openclaw/workspace/myproj/server.js",
  line       = 42,
  condition  = "req && req.url && req.url.includes('/health')",
)

# 3. Continue. Returns when the breakpoint hits OR the program exits OR
#    the default 60s timeout elapses (configurable via wait_for_pause_ms).
pause = debug_session_resume(session_id = sid, action = "continue")

# 4. Inspect. callFrames[0] has callFrameId + url + line; scopeChain[0]
#    is usually the local scope.
top = pause["pause"]["callFrames"][0]
locals = debug_session_get_scope_variables(
  session_id = sid,
  object_id  = top["scopeChain"][0]["objectId"],
)
val = debug_session_evaluate(
  session_id     = sid,
  expression     = "JSON.stringify(req.headers)",
  call_frame_id  = top["callFrameId"],
)

# 5. Step / continue as needed.
debug_session_resume(session_id = sid, action = "step_over")

# 6. When you're done, ALWAYS stop the session — leaving it alive
#    means the inferior keeps running and tying up its inspector port.
debug_session_stop(session_id = sid)
```

## Validating a launch.json for a human developer

Use case: the user asks you to add or fix a `.vscode/launch.json`
config for their project, and they want assurance it actually
attaches before they commit.

1. Read or write `<project>/.vscode/launch.json`. The schema lives
   at https://code.visualstudio.com/docs/editor/launch-json-schema —
   you can parse it with a JSON schema validator or just sanity-check
   the required fields (`type`, `request`, and the request-specific
   fields like `program` / `port`).
2. For a `request: "launch"` config: pull `program`, `args`, `cwd`,
   `env` out of the config and call `debug_session_start` with the
   same values. If the session reaches `state: "paused"` on entry,
   the launch path works.
3. For a `request: "attach"` config: spawn the target separately
   with `node --inspect=127.0.0.1:<port>`, then call
   `debug_session_start` against the same port. (The current MCP
   always uses `--inspect-brk`; for pure attach-to-existing,
   shell-out to a one-shot CDP probe is simpler — see Caveats.)
4. Set one breakpoint at the program's entry point, resume, confirm
   the breakpoint hits, then `stop`. That proves the debugger
   round-trip works.
5. Commit the launch.json (with the user's sign-off if it's their
   repo).

## Caveats

- **`--inspect-brk` only.** The MCP always launches with
  `--inspect-brk`, so the program pauses before any user code.
  Pure "attach to an already-running process" is not yet a tool —
  if you need it, shell out:
  `node -e 'const CDP=require("/opt/debug-mcp/node_modules/chrome-remote-interface"); ...'`
- **Inspector port allocation.** The MCP picks `127.0.0.1:9230+` per
  session. Two MCP processes (e.g. if you ever spawn the debug-mcp
  twice) would step on each other; just stick to the gateway-spawned
  instance.
- **Source maps.** The inferior is launched with
  `--enable-source-maps`. For TypeScript projects, breakpoints set
  on the `.ts` file should hit after the source map applies.
  `pauseDetail.callFrames[i].url` reports the resolved (.ts) path.
- **`/node_modules/` and `<node_internals>` are blackboxed**, so
  stepping skips into them. Override per-session is not exposed yet
  — file an issue if you need it.
- **Output capture is post-hoc.** Tool calls don't stream the
  inferior's stdout/stderr; use `debug_session_consume_output` to
  pull what's accumulated between two snapshots.
- **Always `debug_session_stop` when done.** The MCP traps
  SIGTERM/SIGINT/stdin-close, but a session that you forget to
  stop leaks the inferior process + inspector port until the MCP
  itself terminates.

## When to fall back to the browser-driven code-server UI

- You need to **render variables visually** (large objects with
  collapsible trees, hover popups, treeviews of stack scopes).
- You're testing an **extension** or an editor-side feature (CodeLens,
  decorations, breakpoint glyphs).
- The bug **only reproduces inside the editor** (e.g. tied to the
  editor's runtime, not the program's).

For everything else — programmatic stepping, breakpoint validation,
launch.json round-trip — prefer this MCP. Each Chromium-driven
screenshot costs an extra LLM re-read; the MCP returns structured
JSON.

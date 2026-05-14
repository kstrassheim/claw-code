#!/usr/bin/env node
// debug-mcp: stdio MCP that drives the V8 inspector (CDP) for Node
// programs on behalf of the openclaw bot. Designed for the bot's
// "fix a debugger config / verify a launch.json attaches" workflow:
// the agent calls debug_session_start, sets breakpoints, drives the
// inferior, inspects state, and stops cleanly.
//
// Node-first by design — the openclaw runtime and most sandbox
// projects are Node/TypeScript. Python (debugpy/DAP) is intentionally
// not implemented in this version; the bot can still shell out to
// `python -m pdb` for ad-hoc Python work.
//
// Out-of-scope: source maps (V8 inspector handles them automatically
// when the program is run with `--enable-source-maps` or a recent
// Node), conditional logpoints, exception breakpoints. Adding them is
// a small CDP call away if the bot ever needs it.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import CDP from "chrome-remote-interface";

// ---------- session registry ----------

const sessions = new Map(); // id -> NodeDebugSession
let nextInspectorPort = 9230;

function allocatePort() {
  return nextInspectorPort++;
}

class NodeDebugSession {
  constructor({ program, args = [], cwd, env = {}, stopOnEntry = true, sourceMaps = true }) {
    this.id = randomUUID();
    this.kind = "node";
    this.program = program;
    this.args = args;
    this.cwd = cwd;
    this.env = env;
    this.stopOnEntry = stopOnEntry;
    this.sourceMaps = sourceMaps;
    this.state = "starting"; // starting | running | paused | terminated
    this.exitCode = null;
    this.lastPause = null;
    this.breakpoints = new Map(); // bpId -> { file, line, condition }
    this.proc = null;
    this.cdp = null;
    this.port = null;
    this._pauseWaiters = []; // pending Promise resolvers for "next pause"
    this.stdoutBuf = [];
    this.stderrBuf = [];
  }

  async start() {
    this.port = allocatePort();
    const inspectFlag = this.stopOnEntry ? "--inspect-brk" : "--inspect";
    const nodeArgs = [`${inspectFlag}=127.0.0.1:${this.port}`];
    if (this.sourceMaps) nodeArgs.push("--enable-source-maps");
    nodeArgs.push(this.program, ...this.args);

    this.proc = spawn("node", nodeArgs, {
      cwd: this.cwd,
      env: { ...process.env, ...this.env },
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.proc.stdout.on("data", (d) => this.stdoutBuf.push(d.toString()));
    this.proc.stderr.on("data", (d) => this.stderrBuf.push(d.toString()));
    this.proc.on("exit", (code, signal) => {
      this.state = "terminated";
      this.exitCode = code ?? (signal ? `signal:${signal}` : null);
      this._flushWaiters(new Error(`Process exited (code=${this.exitCode})`));
    });

    // Poll until the inspector accepts a CDP connection. Node logs
    // "Debugger listening on ws://..." on stderr but we don't parse
    // that — polling is simpler and works for any Node version.
    let lastErr;
    for (let i = 0; i < 100; i++) {
      try {
        this.cdp = await CDP({ host: "127.0.0.1", port: this.port });
        break;
      } catch (e) {
        lastErr = e;
        await delay(100);
      }
    }
    if (!this.cdp) {
      this._killProc();
      throw new Error(`inspector not reachable on 127.0.0.1:${this.port} after 10s: ${lastErr?.message ?? lastErr}`);
    }

    const { Debugger, Runtime } = this.cdp;
    Debugger.paused((params) => this._onPaused(params));
    Debugger.resumed(() => {
      this.state = "running";
    });
    await Runtime.enable();
    await Debugger.enable();
    // Tell V8 to honour source maps when reporting locations (defaults
    // to true in modern Node but be explicit).
    try { await Debugger.setBlackboxPatterns({ patterns: ["/node_modules/", "<node_internals>"] }); } catch {}
    // runIfWaitingForDebugger lets us proceed once the agent has set
    // breakpoints; we still get a paused event for --inspect-brk first.
    await Runtime.runIfWaitingForDebugger();

    if (this.stopOnEntry) {
      // Wait for the initial pause-on-entry (or a short timeout if it
      // already fired before we attached the handler — should not happen
      // with -brk but be robust).
      try {
        await this._waitForPause(5000);
      } catch (e) {
        // Inferior may have a tiny script that finished before we caught
        // the pause event. Don't fail start() for that — the agent will
        // see state=terminated and the output.
      }
    } else {
      this.state = "running";
    }

    return this.summary();
  }

  _onPaused(params) {
    this.state = "paused";
    this.lastPause = {
      reason: params.reason,
      hitBreakpoints: params.hitBreakpoints ?? [],
      callFrames: (params.callFrames ?? []).map((f) => ({
        callFrameId: f.callFrameId,
        functionName: f.functionName || "(anonymous)",
        url: f.url,
        line: f.location.lineNumber,
        column: f.location.columnNumber,
        scriptId: f.location.scriptId,
        scopeChain: (f.scopeChain ?? []).map((s) => ({
          type: s.type,
          name: s.name,
          objectId: s.object?.objectId,
        })),
      })),
    };
    this._flushWaiters(null, this.lastPause);
  }

  _flushWaiters(err, value) {
    const waiters = this._pauseWaiters;
    this._pauseWaiters = [];
    for (const w of waiters) {
      if (err) w.reject(err);
      else w.resolve(value);
    }
  }

  _waitForPause(timeoutMs = 60000) {
    return new Promise((resolve, reject) => {
      const entry = { resolve, reject };
      const timer = setTimeout(() => {
        const idx = this._pauseWaiters.indexOf(entry);
        if (idx !== -1) this._pauseWaiters.splice(idx, 1);
        reject(new Error(`no pause within ${timeoutMs}ms (state=${this.state})`));
      }, timeoutMs);
      entry.resolve = (v) => { clearTimeout(timer); resolve(v); };
      entry.reject = (e) => { clearTimeout(timer); reject(e); };
      this._pauseWaiters.push(entry);
    });
  }

  async resume({ action = "continue", waitForPauseMs = 60000 } = {}) {
    if (this.state === "terminated") throw new Error("session terminated");
    if (this.state !== "paused") throw new Error(`cannot resume from state=${this.state}`);
    const { Debugger } = this.cdp;
    this.state = "running";
    this.lastPause = null;
    switch (action) {
      case "continue":   await Debugger.resume();    break;
      case "step_over":  await Debugger.stepOver();  break;
      case "step_into":  await Debugger.stepInto();  break;
      case "step_out":   await Debugger.stepOut();   break;
      default: throw new Error(`unknown action: ${action}`);
    }
    if (waitForPauseMs > 0) {
      try {
        const pause = await this._waitForPause(waitForPauseMs);
        return { state: this.state, pause };
      } catch (e) {
        return { state: this.state, note: e.message };
      }
    }
    return { state: this.state };
  }

  async setBreakpoint({ file, line, column = 0, condition }) {
    if (!this.cdp) throw new Error("session not started");
    const { Debugger } = this.cdp;
    const url = file.startsWith("file://") ? file : `file://${file}`;
    const { breakpointId, locations } = await Debugger.setBreakpointByUrl({
      url,
      lineNumber: Math.max(0, line - 1), // CDP is 0-indexed; the bot speaks 1-indexed
      columnNumber: column,
      condition,
    });
    this.breakpoints.set(breakpointId, { file, line, column, condition, locations });
    return { id: breakpointId, file, line, column, locations };
  }

  async removeBreakpoint(id) {
    const { Debugger } = this.cdp;
    await Debugger.removeBreakpoint({ breakpointId: id });
    this.breakpoints.delete(id);
  }

  listBreakpoints() {
    return [...this.breakpoints.entries()].map(([id, bp]) => ({ id, ...bp }));
  }

  async evaluate({ expression, callFrameId, returnByValue = true }) {
    if (!this.cdp) throw new Error("session not started");
    const { Debugger, Runtime } = this.cdp;
    if (callFrameId) {
      const res = await Debugger.evaluateOnCallFrame({
        callFrameId,
        expression,
        returnByValue,
        generatePreview: true,
      });
      return { result: res.result, exceptionDetails: res.exceptionDetails };
    }
    // No frame selected: eval in global scope of the inferior. This
    // works whether or not the program is paused (CDP runs it in the
    // microtask after current frame).
    const res = await Runtime.evaluate({ expression, returnByValue, generatePreview: true });
    return { result: res.result, exceptionDetails: res.exceptionDetails };
  }

  async getScopeVariables({ objectId }) {
    if (!this.cdp) throw new Error("session not started");
    const { Runtime } = this.cdp;
    const res = await Runtime.getProperties({
      objectId,
      ownProperties: true,
      generatePreview: true,
    });
    return res.result.map((p) => ({
      name: p.name,
      value: p.value
        ? {
            type: p.value.type,
            subtype: p.value.subtype,
            description: p.value.description,
            value: p.value.value,
            objectId: p.value.objectId,
          }
        : null,
    }));
  }

  consumeOutput() {
    const out = this.stdoutBuf.join("");
    const err = this.stderrBuf.join("");
    this.stdoutBuf = [];
    this.stderrBuf = [];
    return { stdout: out, stderr: err };
  }

  async stop() {
    if (this.state === "terminated") return;
    this._killProc();
    try { await this.cdp?.close(); } catch {}
    this.state = "terminated";
    this._flushWaiters(new Error("session stopped"));
  }

  _killProc() {
    if (!this.proc) return;
    if (this.proc.exitCode === null && this.proc.signalCode === null) {
      try { this.proc.kill("SIGTERM"); } catch {}
      setTimeout(() => { try { this.proc.kill("SIGKILL"); } catch {} }, 2000).unref();
    }
  }

  summary() {
    return {
      id: this.id,
      kind: this.kind,
      state: this.state,
      program: this.program,
      args: this.args,
      cwd: this.cwd,
      exitCode: this.exitCode,
      breakpoints: [...this.breakpoints.keys()],
      pause: this.lastPause
        ? {
            reason: this.lastPause.reason,
            top: this.lastPause.callFrames[0]
              ? {
                  functionName: this.lastPause.callFrames[0].functionName,
                  url: this.lastPause.callFrames[0].url,
                  line: (this.lastPause.callFrames[0].line ?? 0) + 1,
                }
              : null,
          }
        : null,
    };
  }
}

// ---------- MCP tool surface ----------

function ok(payload) {
  return { content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] };
}
function err(message) {
  return { isError: true, content: [{ type: "text", text: `error: ${message}` }] };
}

function getSession(args) {
  const s = sessions.get(args.session_id);
  if (!s) throw new Error(`no such session: ${args.session_id}`);
  return s;
}

const TOOLS = [
  {
    name: "debug_session_start",
    description:
      "Spawn a Node program under the V8 inspector and attach. Returns the session id plus the initial pause state (if stop_on_entry).",
    inputSchema: {
      type: "object",
      required: ["language", "program"],
      properties: {
        language: { type: "string", enum: ["node"], description: "Only `node` is implemented in v1." },
        program: { type: "string", description: "Path to the script to debug (relative to cwd or absolute)." },
        args: { type: "array", items: { type: "string" }, description: "argv to pass to the program." },
        cwd: { type: "string", description: "Working directory. Defaults to the agent's pwd." },
        env: { type: "object", description: "Extra env vars merged into the inferior's environment." },
        stop_on_entry: { type: "boolean", default: true, description: "Pause on the first line so you can set breakpoints." },
      },
    },
    handler: async (a) => {
      if (a.language !== "node") return err(`language=${a.language} not implemented; v1 supports node only`);
      const s = new NodeDebugSession({
        program: a.program,
        args: a.args ?? [],
        cwd: a.cwd,
        env: a.env ?? {},
        stopOnEntry: a.stop_on_entry ?? true,
      });
      sessions.set(s.id, s);
      try {
        await s.start();
        return ok(s.summary());
      } catch (e) {
        sessions.delete(s.id);
        return err(e.message);
      }
    },
  },
  {
    name: "debug_session_list",
    description: "List active debug sessions.",
    inputSchema: { type: "object", properties: {} },
    handler: async () => ok([...sessions.values()].map((s) => s.summary())),
  },
  {
    name: "debug_session_state",
    description: "Current state of a session (running / paused / terminated) plus the most recent pause snapshot if paused.",
    inputSchema: {
      type: "object", required: ["session_id"],
      properties: { session_id: { type: "string" } },
    },
    handler: async (a) => {
      try {
        const s = getSession(a);
        return ok({ ...s.summary(), pauseDetail: s.lastPause });
      } catch (e) { return err(e.message); }
    },
  },
  {
    name: "debug_session_resume",
    description: "Resume execution. Returns either the next pause snapshot or a note if no pause occurred within the timeout.",
    inputSchema: {
      type: "object", required: ["session_id"],
      properties: {
        session_id: { type: "string" },
        action: { type: "string", enum: ["continue", "step_over", "step_into", "step_out"], default: "continue" },
        wait_for_pause_ms: { type: "integer", default: 60000, description: "Max ms to wait for the next pause / program exit before returning." },
      },
    },
    handler: async (a) => {
      try {
        const s = getSession(a);
        const out = await s.resume({ action: a.action ?? "continue", waitForPauseMs: a.wait_for_pause_ms ?? 60000 });
        return ok(out);
      } catch (e) { return err(e.message); }
    },
  },
  {
    name: "debug_session_set_breakpoint",
    description: "Set a breakpoint by file path + 1-indexed line. Optional condition (JS expression evaluated in the frame).",
    inputSchema: {
      type: "object", required: ["session_id", "file", "line"],
      properties: {
        session_id: { type: "string" },
        file: { type: "string", description: "Absolute path to the source file." },
        line: { type: "integer", minimum: 1 },
        column: { type: "integer", minimum: 0, default: 0 },
        condition: { type: "string", description: "Optional JS expression; the breakpoint only fires when it evaluates truthy." },
      },
    },
    handler: async (a) => {
      try {
        const s = getSession(a);
        return ok(await s.setBreakpoint({ file: a.file, line: a.line, column: a.column ?? 0, condition: a.condition }));
      } catch (e) { return err(e.message); }
    },
  },
  {
    name: "debug_session_remove_breakpoint",
    description: "Remove a breakpoint by id (the id returned from set_breakpoint).",
    inputSchema: {
      type: "object", required: ["session_id", "breakpoint_id"],
      properties: { session_id: { type: "string" }, breakpoint_id: { type: "string" } },
    },
    handler: async (a) => {
      try {
        const s = getSession(a);
        await s.removeBreakpoint(a.breakpoint_id);
        return ok({ removed: a.breakpoint_id });
      } catch (e) { return err(e.message); }
    },
  },
  {
    name: "debug_session_list_breakpoints",
    description: "List active breakpoints for a session.",
    inputSchema: {
      type: "object", required: ["session_id"],
      properties: { session_id: { type: "string" } },
    },
    handler: async (a) => {
      try { return ok(getSession(a).listBreakpoints()); }
      catch (e) { return err(e.message); }
    },
  },
  {
    name: "debug_session_evaluate",
    description:
      "Evaluate a JS expression. If call_frame_id is provided (from a pause snapshot), runs in that frame's scope; otherwise runs in the inferior's global scope.",
    inputSchema: {
      type: "object", required: ["session_id", "expression"],
      properties: {
        session_id: { type: "string" },
        expression: { type: "string" },
        call_frame_id: { type: "string", description: "callFrameId from pauseDetail.callFrames[i]." },
        return_by_value: { type: "boolean", default: true },
      },
    },
    handler: async (a) => {
      try {
        const s = getSession(a);
        return ok(await s.evaluate({
          expression: a.expression,
          callFrameId: a.call_frame_id,
          returnByValue: a.return_by_value ?? true,
        }));
      } catch (e) { return err(e.message); }
    },
  },
  {
    name: "debug_session_get_scope_variables",
    description:
      "Resolve a scope object (from pauseDetail.callFrames[i].scopeChain[j].objectId) to its variables. Lets you inspect locals/closures without writing eval expressions.",
    inputSchema: {
      type: "object", required: ["session_id", "object_id"],
      properties: { session_id: { type: "string" }, object_id: { type: "string" } },
    },
    handler: async (a) => {
      try { return ok(await getSession(a).getScopeVariables({ objectId: a.object_id })); }
      catch (e) { return err(e.message); }
    },
  },
  {
    name: "debug_session_consume_output",
    description: "Drain stdout/stderr collected from the inferior since the last consume call. Useful to inspect program output around a breakpoint.",
    inputSchema: {
      type: "object", required: ["session_id"],
      properties: { session_id: { type: "string" } },
    },
    handler: async (a) => {
      try { return ok(getSession(a).consumeOutput()); }
      catch (e) { return err(e.message); }
    },
  },
  {
    name: "debug_session_stop",
    description: "Terminate the inferior and close the CDP connection. Idempotent.",
    inputSchema: {
      type: "object", required: ["session_id"],
      properties: { session_id: { type: "string" } },
    },
    handler: async (a) => {
      const s = sessions.get(a.session_id);
      if (!s) return ok({ note: "already gone" });
      await s.stop();
      sessions.delete(s.id);
      return ok({ stopped: s.id });
    },
  },
];

// ---------- wire to MCP ----------

const server = new Server(
  { name: "debug-mcp", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: TOOLS.map((t) => ({ name: t.name, description: t.description, inputSchema: t.inputSchema })),
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const tool = TOOLS.find((t) => t.name === req.params.name);
  if (!tool) return err(`unknown tool: ${req.params.name}`);
  try { return await tool.handler(req.params.arguments ?? {}); }
  catch (e) { return err(e.message ?? String(e)); }
});

// Tear down everything when the parent (the openclaw gateway) closes
// our stdio. Without this an orphaned inferior + inspector would leak
// across MCP restarts.
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
process.stdin.on("end", shutdown);
async function shutdown() {
  for (const s of sessions.values()) {
    try { await s.stop(); } catch {}
  }
  process.exit(0);
}

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[debug-mcp] ready");

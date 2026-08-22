#!/usr/bin/env node
// security-mcp: thin stdio-MCP wrapper around the security scanners
// baked into the image, used by the MR reviewer (and available to every
// agent) for CHECKING code — none of these tools modify anything.
//
//   bandit_scan           — Python SAST (bandit -r, JSON)
//   pip_audit             — Python dependency CVEs (pip-audit, JSON)
//   npm_audit             — JS/TS dependency CVEs (npm audit --json)
//   psscriptanalyzer_scan — PowerShell lint incl. security rules (pwsh)
//   gitleaks_scan         — hardcoded secrets/credentials in a tree
//
// SAST for Python/FastAPI/JS/React beyond bandit lives in the separate
// `semgrep` MCP (official server) — this MCP deliberately does not
// duplicate it.
//
// All tools are read-only scans; "findings found" exit codes (most
// scanners exit 1 when they find something) are treated as a successful
// scan with results, NOT as an error.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { existsSync } from "node:fs";

const execFileP = promisify(execFile);
const SCAN_TIMEOUT_MS = 300_000; // scanners on a big tree can be slow

// Run a scanner. Findings-style exits (code 1..2 with stdout) are a
// successful scan; only a spawn failure / empty-output crash is an error.
async function scan(cmd, args, opts = {}) {
  try {
    const { stdout, stderr } = await execFileP(cmd, args, {
      maxBuffer: 16 * 1024 * 1024,
      timeout: SCAN_TIMEOUT_MS,
      ...opts,
    });
    return { ok: true, stdout, stderr };
  } catch (err) {
    if (err.killed || err.signal)
      return { ok: false, stderr: `scan timed out / killed (${err.signal ?? "timeout"})` };
    if (typeof err.code === "number" && (err.stdout ?? "").trim().length > 0)
      return { ok: true, stdout: err.stdout, stderr: err.stderr ?? "", findingsExit: err.code };
    return {
      ok: false,
      stdout: err.stdout ?? "",
      stderr: err.stderr ?? String(err.message ?? err),
      code: err.code,
    };
  }
}

function asText(res, header = "") {
  if (res.ok) {
    const note = res.findingsExit ? `(scanner exit ${res.findingsExit} — findings present)\n` : "";
    return { content: [{ type: "text", text: `${header}${note}${res.stdout || "(no output — clean)"}` }] };
  }
  return {
    isError: true,
    content: [{ type: "text", text: `error: ${res.stderr || `scanner exit ${res.code}`}` }],
  };
}

function requirePath(p) {
  if (!p || typeof p !== "string") throw new Error("path is required");
  if (!existsSync(p)) throw new Error(`path does not exist: ${p}`);
  return p;
}

const server = new Server(
  { name: "security", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "bandit_scan",
      description: "Python SAST via bandit: recursively scan a directory (or one file) for security issues — injection, weak crypto, subprocess misuse, hardcoded passwords, etc. Returns JSON findings. Use on the Python side (FastAPI backend) of a change.",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "Absolute directory or file to scan." },
          severity: { type: "string", enum: ["low", "medium", "high"], description: "Minimum severity to report (default low = everything)." },
          confidence: { type: "string", enum: ["low", "medium", "high"], description: "Minimum confidence to report (default low)." },
        },
        required: ["path"],
      },
    },
    {
      name: "pip_audit",
      description: "Known-CVE audit of Python dependencies via pip-audit. Point it at a requirements file. Returns JSON with vulnerable packages, ids (CVE/GHSA), and fix versions.",
      inputSchema: {
        type: "object",
        properties: {
          requirements: { type: "string", description: "Absolute path to a requirements.txt (or compatible) file." },
        },
        required: ["requirements"],
      },
    },
    {
      name: "npm_audit",
      description: "Known-CVE audit of JS/TS dependencies via `npm audit --json`, run in the given project directory (needs package.json + a lockfile). Returns the JSON vulnerability report.",
      inputSchema: {
        type: "object",
        properties: {
          cwd: { type: "string", description: "Absolute path of the directory containing package.json / package-lock.json." },
          omit_dev: { type: "boolean", description: "Audit production dependencies only (default false: audit everything)." },
        },
        required: ["cwd"],
      },
    },
    {
      name: "psscriptanalyzer_scan",
      description: "PowerShell static analysis via PSScriptAnalyzer (all rules, including the security rules: plaintext passwords, Invoke-Expression, ConvertTo-SecureString misuse, ...). Scans a .ps1/.psm1 file or a directory recursively. Returns JSON diagnostics.",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "Absolute path of a PowerShell file or a directory." },
          severity: {
            type: "array", items: { type: "string", enum: ["Information", "Warning", "Error"] },
            description: "Severities to report (default Warning + Error).",
          },
        },
        required: ["path"],
      },
    },
    {
      name: "gitleaks_scan",
      description: "Secret detection via gitleaks: scan a directory tree for hardcoded credentials, API keys, tokens and private keys. Returns JSON findings (empty = clean). Use on the checkout of the branch under review. NOTE: this checks the files AS THEY ARE NOW — a secret committed and then deleted again is invisible here; use gitleaks_git_scan for that.",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "Absolute directory to scan (working tree; .git history is not scanned)." },
        },
        required: ["path"],
      },
    },
    {
      name: "gitleaks_git_scan",
      description: "Secret detection in GIT HISTORY via `gitleaks git`: scans commit contents, so it catches credentials that were committed and later 'removed' — they still live in history and ship on merge. For an MR review, pass log_opts like 'origin/main..HEAD' to scan exactly the commits the MR adds; omit log_opts to scan the full history. Returns JSON findings with the offending commit per finding.",
      inputSchema: {
        type: "object",
        properties: {
          path: { type: "string", description: "Absolute path of the git repository (checkout root)." },
          log_opts: { type: "string", description: "git log options restricting the range, e.g. 'origin/main..HEAD' (recommended for MR review). Omit for full history." },
        },
        required: ["path"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: a = {} } = req.params;
  try {
    switch (name) {
      case "bandit_scan": {
        const p = requirePath(a.path);
        const args = ["-r", p, "-f", "json", "-q"];
        const sevFlag = { low: "-l", medium: "-ll", high: "-lll" }[a.severity];
        if (sevFlag) args.push(sevFlag);
        const confFlag = { low: "-i", medium: "-ii", high: "-iii" }[a.confidence];
        if (confFlag) args.push(confFlag);
        // Never descend into virtualenvs / node_modules — noise + slow.
        args.push("-x", "venv,.venv,node_modules,dist,build,.git");
        return asText(await scan("bandit", args));
      }
      case "pip_audit": {
        const r = requirePath(a.requirements);
        return asText(await scan("pip-audit", ["-r", r, "-f", "json", "--progress-spinner", "off"]));
      }
      case "npm_audit": {
        const cwd = requirePath(a.cwd);
        const args = ["audit", "--json"];
        if (a.omit_dev) args.push("--omit", "dev");
        return asText(await scan("npm", args, { cwd }));
      }
      case "psscriptanalyzer_scan": {
        const p = requirePath(a.path);
        const sev = Array.isArray(a.severity) && a.severity.length ? a.severity : ["Warning", "Error"];
        const sevList = sev.map((s) => `'${s.replace(/[^A-Za-z]/g, "")}'`).join(",");
        // -Recurse is ignored for a single file; safe to always pass.
        const ps = `Invoke-ScriptAnalyzer -Path '${p.replace(/'/g, "''")}' -Recurse -Severity ${sevList} | ` +
          `Select-Object RuleName,Severity,ScriptPath,Line,Message | ConvertTo-Json -Depth 4`;
        return asText(await scan("pwsh", ["-NoProfile", "-NonInteractive", "-Command", ps]));
      }
      case "gitleaks_scan": {
        const p = requirePath(a.path);
        // `dir` scans the working tree (no git history); JSON to stdout.
        return asText(await scan("gitleaks", [
          "dir", p, "--no-banner", "--report-format", "json", "--report-path", "/dev/stdout",
        ]));
      }
      case "gitleaks_git_scan": {
        const p = requirePath(a.path);
        const args = ["git", p, "--no-banner", "--report-format", "json", "--report-path", "/dev/stdout"];
        if (a.log_opts && typeof a.log_opts === "string") args.push("--log-opts", a.log_opts);
        return asText(await scan("gitleaks", args));
      }
      default:
        return { isError: true, content: [{ type: "text", text: `unknown tool: ${name}` }] };
    }
  } catch (e) {
    return { isError: true, content: [{ type: "text", text: `error: ${e.message ?? e}` }] };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[security-mcp] ready");

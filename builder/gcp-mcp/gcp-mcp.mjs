#!/usr/bin/env node
// gcp-mcp: thin stdio-MCP wrapper around `gcloud`. Auth comes from
// the standard credential chain (env: GOOGLE_APPLICATION_CREDENTIALS
// pointing at a service-account JSON, OR an interactive
// `gcloud auth login` cache under ~/.config/gcloud/). The MCP does
// not run `auth login` or `auth revoke` on behalf of the agent —
// both are refused at this layer.
//
// Tool surface mirrors azure-mcp: identity, projects, resource
// inventory (Cloud Asset API), IAM policy view for the active
// principal, plus an escape-hatch gcp_run. Defense-in-depth blocks
// secret-manager value access, KMS crypto ops, IAM mutation, and
// any `auth` subcommand regardless of the live IAM bindings.
//
// Creds are NOT wired by the chart in this revision. Any tool that
// needs auth will return "ERROR: Reauthentication required" or
// similar until a SA JSON is sealed into openclaw-secrets and
// mounted at GOOGLE_APPLICATION_CREDENTIALS.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileP = promisify(execFile);

function refuses(args) {
  const flat = args.join(" ").toLowerCase();
  if (/\bsecrets\s+versions\s+access\b/.test(flat))
    return "secret-manager value access is blocked at the MCP layer.";
  if (/\bsecrets\s+(create|update|delete|add-iam-policy-binding|set-iam-policy)\b/.test(flat))
    return "secret-manager mutation is blocked at the MCP layer.";
  if (/\bkms\b\s+(keys|keyrings|encrypt|decrypt|sign|verify|mac-sign|mac-verify)\b\s+(create|update|destroy|import|encrypt|decrypt|sign|verify)/.test(flat))
    return "kms cryptographic / key-mutation operations are blocked at the MCP layer.";
  if (/\biam\b\s+(service-accounts|members|policy-bindings)\s+(create|update|delete|add|remove|set)/.test(flat))
    return "iam mutation is blocked at the MCP layer.";
  if (/\bauth\s+(login|application-default\s+login|revoke|application-default\s+revoke|print-access-token|print-identity-token)\b/.test(flat))
    return "auth login/revoke/print-token is blocked at the MCP layer.";
  if (/\b(impersonate-service-account|--impersonate-service-account)\b/.test(flat))
    return "service-account impersonation is blocked at the MCP layer.";
  return null;
}

async function gcloud(args, opts = {}) {
  const refusal = refuses(args);
  if (refusal) return { ok: false, stderr: refusal };
  try {
    const { stdout, stderr } = await execFileP("gcloud", args, {
      maxBuffer: 8 * 1024 * 1024,
      ...opts,
    });
    return { ok: true, stdout, stderr };
  } catch (err) {
    return {
      ok: false,
      stdout: err.stdout ?? "",
      stderr: err.stderr ?? String(err.message ?? err),
      code: err.code,
    };
  }
}

function asText(res) {
  if (res.ok) return { content: [{ type: "text", text: res.stdout || "(no output)" }] };
  return {
    isError: true,
    content: [{ type: "text", text: `error: ${res.stderr || `gcloud exit ${res.code}`}` }],
  };
}

const server = new Server(
  { name: "gcp", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "gcp_account",
      description: "Who am I — the active gcloud account, project, and credentialed identities.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "gcp_projects",
      description: "List GCP projects the bot's identity can see.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "gcp_resources",
      description: "Cross-service inventory via Cloud Asset API (`gcloud asset search-all-resources`). The Cloud Asset API must be enabled on the searched scope and the principal must have `cloudasset.assets.searchAllResources`.",
      inputSchema: {
        type: "object",
        properties: {
          scope: { type: "string", description: "Scope to search — e.g. `projects/PROJECT_ID`, `folders/FOLDER_ID`, or `organizations/ORG_ID`. Defaults to the active project." },
          asset_types: { type: "array", items: { type: "string" }, description: "Filter by asset types (e.g. ['compute.googleapis.com/Instance'])." },
          query: { type: "string", description: "AIP-160 filter expression, e.g. 'state:RUNNING'." },
        },
      },
    },
    {
      name: "gcp_my_iam_bindings",
      description: "Which IAM roles the bot's identity holds on a given project. Pass project_id explicitly or omit to use the active project.",
      inputSchema: {
        type: "object",
        properties: {
          project_id: { type: "string", description: "Project to inspect; defaults to active." },
        },
      },
    },
    {
      name: "gcp_run",
      description: "Escape hatch: invoke `gcloud <args>` with arbitrary arguments. The MCP rejects secret-value access, KMS crypto, IAM mutation, auth subcommands, and SA impersonation regardless of the live IAM bindings.",
      inputSchema: {
        type: "object",
        properties: {
          args: { type: "array", items: { type: "string" }, description: "argv after the `gcloud` binary, e.g. ['compute','instances','list','--format=json']." },
        },
        required: ["args"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: a } = req.params;
  switch (name) {
    case "gcp_account": {
      // Compose: active account+project from `config list`, plus the
      // list of credentialed accounts. Two calls so the agent sees
      // both at once.
      const cfg = await gcloud(["config", "list", "--format", "json"]);
      const auth = await gcloud(["auth", "list", "--format", "json"]);
      const merged = {
        config: cfg.ok ? safeJSON(cfg.stdout) : { error: cfg.stderr },
        auth_list: auth.ok ? safeJSON(auth.stdout) : { error: auth.stderr },
      };
      return { content: [{ type: "text", text: JSON.stringify(merged, null, 2) }] };
    }
    case "gcp_projects":
      return asText(await gcloud(["projects", "list", "--format", "json"]));
    case "gcp_resources": {
      const args = ["asset", "search-all-resources", "--format", "json"];
      if (a.scope) args.push("--scope", a.scope);
      if (Array.isArray(a.asset_types) && a.asset_types.length) args.push("--asset-types", a.asset_types.join(","));
      if (a.query) args.push("--query", a.query);
      return asText(await gcloud(args));
    }
    case "gcp_my_iam_bindings": {
      // Resolve the active account, then dump the project's IAM policy
      // filtered to bindings involving that member.
      const acct = await gcloud(["config", "get-value", "account"]);
      if (!acct.ok) return asText(acct);
      const member = acct.stdout.trim();
      const proj = a.project_id ?? (await gcloud(["config", "get-value", "project"])).stdout?.trim();
      if (!proj) return { isError: true, content: [{ type: "text", text: "no active project; pass project_id." }] };
      const filter = `bindings.members:${member.includes("@") && !member.includes(":") ? `user:${member}` : member}`;
      return asText(await gcloud(["projects", "get-iam-policy", proj, "--flatten", "bindings[].members",
        "--filter", filter, "--format", "json"]));
    }
    case "gcp_run":
      return asText(await gcloud(a.args));
    default:
      return { isError: true, content: [{ type: "text", text: `unknown tool: ${name}` }] };
  }
});

function safeJSON(s) {
  try { return JSON.parse(s); } catch { return { raw: s }; }
}

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[gcp-mcp] ready");

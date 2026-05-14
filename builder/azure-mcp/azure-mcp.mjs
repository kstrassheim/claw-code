#!/usr/bin/env node
// azure-mcp: thin stdio-MCP wrapper around `az` CLI, using the existing
// ~/.azure/ token cache (PVC-persistent under subPath dotazure). Same
// RBAC as the underlying Entra user account — Azure rejects whatever
// the user's role doesn't permit, and the MCP surfaces the rejection
// unchanged. No privilege escalation, no token munging.
//
// Tools cover the common dev-inspection loop:
//   - account introspection
//   - resource group / resource listing
//   - resource detail fetch
//   - role-assignment listing (what can I actually do?)
// Plus an escape-hatch `az_run` that forwards arbitrary args (still
// gated by the user's Azure RBAC). Anything more exotic the agent
// still has direct `az` shell access for.
//
// Explicit guard: any `az ... secret ...` or `az keyvault secret ...`
// invocation is rejected at the MCP layer even before az sees it.
// This is defense-in-depth on top of the "no keyvault role" RBAC
// assumption; if the human later grants the bot KeyVault role, the
// MCP still won't read secrets via these tools (CLI fallback works
// but is loud).

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileP = promisify(execFile);

function refusesSecrets(args) {
  const flat = args.join(" ").toLowerCase();
  if (/\bkeyvault\b\s+secret\b/.test(flat)) return "keyvault secret access is blocked at the MCP layer.";
  if (/\baz\s+keyvault\s+secret/.test(flat)) return "keyvault secret access is blocked at the MCP layer.";
  if (/\bsecret\s+(show|list|set|update|delete)\b/.test(flat)) return "secret access is blocked at the MCP layer.";
  return null;
}

async function az(args, opts = {}) {
  const refusal = refusesSecrets(args);
  if (refusal) return { ok: false, stderr: refusal };
  try {
    const { stdout, stderr } = await execFileP("az", args, {
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
    content: [{ type: "text", text: `error: ${res.stderr || `az exit ${res.code}`}` }],
  };
}

const server = new Server(
  { name: "azure", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "az_account_show",
      description: "Show the currently-active Azure subscription + tenant + user.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "az_account_list",
      description: "List every subscription the bot has any role on.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "az_group_list",
      description: "List resource groups (in the active subscription).",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "az_resource_list",
      description: "List resources, optionally filtered by group / type / name pattern.",
      inputSchema: {
        type: "object",
        properties: {
          group: { type: "string", description: "Resource group name filter." },
          type: { type: "string", description: "Resource type filter, e.g. Microsoft.Web/sites." },
          name: { type: "string", description: "Name substring filter (case-insensitive)." },
        },
      },
    },
    {
      name: "az_resource_show",
      description: "Fetch full JSON for one resource by ID (the `id` field from az_resource_list output).",
      inputSchema: {
        type: "object",
        properties: {
          id: { type: "string" },
        },
        required: ["id"],
      },
    },
    {
      name: "az_role_assignments",
      description: "List the role assignments the bot's identity holds. Useful when an operation gets Forbidden and you want to know what scopes you actually have.",
      inputSchema: {
        type: "object",
        properties: {
          scope: { type: "string", description: "Optional scope filter (subscription id or resource id)." },
        },
      },
    },
    {
      name: "az_run",
      description: "Escape hatch: invoke `az <args>` with arbitrary arguments. Output is whatever `az` prints. Useful for subcommands not covered by the typed tools above. Secret-related subcommands are blocked at this layer even if the bot's RBAC would otherwise permit them.",
      inputSchema: {
        type: "object",
        properties: {
          args: {
            type: "array",
            items: { type: "string" },
            description: "argv after the `az` binary, e.g. ['webapp','list','--query','[].{name:name,state:state}'].",
          },
        },
        required: ["args"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: a } = req.params;
  switch (name) {
    case "az_account_show":
      return asText(await az(["account", "show", "--output", "json"]));
    case "az_account_list":
      return asText(await az(["account", "list", "--output", "json"]));
    case "az_group_list":
      return asText(await az(["group", "list", "--output", "json"]));
    case "az_resource_list": {
      const args = ["resource", "list", "--output", "json"];
      if (a.group) args.push("--resource-group", a.group);
      if (a.type) args.push("--resource-type", a.type);
      if (a.name) args.push("--name", a.name);
      return asText(await az(args));
    }
    case "az_resource_show":
      return asText(await az(["resource", "show", "--ids", a.id, "--output", "json"]));
    case "az_role_assignments": {
      const args = ["role", "assignment", "list", "--all", "--output", "json"];
      if (a.scope) args.push("--scope", a.scope);
      return asText(await az(args));
    }
    case "az_run":
      return asText(await az(a.args));
    default:
      return { isError: true, content: [{ type: "text", text: `unknown tool: ${name}` }] };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[azure-mcp] ready");

#!/usr/bin/env node
// argocd-mcp: thin stdio-MCP wrapper around `argocd` CLI, scoped to
// whatever the openclaw-dev ArgoCD account permits (sandbox apps
// only, read+sync, no create/delete/update). Same pattern as
// k8s-mcp / azure-mcp.
//
// Auth: ARGOCD_AUTH_TOKEN + ARGOCD_SERVER env vars are inherited from
// the pod env (sealed into openclaw-secrets via the workflow). argocd
// CLI reads both env names natively.
//
// Server connection: argocd-server is deployed with --insecure (no
// TLS) and --rootpath=/argocd/ (see k8s-deployment/argocd/values.yaml).
// Every CLI invocation needs --plaintext --grpc-web --grpc-web-root-path
// /argocd; bake those into a shared flag list.
//
// Defense-in-depth: even though the bot's ArgoCD RBAC denies the
// destructive verbs, the MCP also doesn't expose `app create`,
// `app delete`, or `app set` tools. If a future RBAC bug grants
// `applications, create`, the MCP path still can't trigger it.
// Escape hatch (`argocd_run`) inherits the same scope-deny.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileP = promisify(execFile);

// Server addr comes from env (set by the deployment's envFrom secret).
// Default matches the openclaw cluster's argocd-server svc — useful
// only as a fallback; in normal operation $ARGOCD_SERVER is set.
const SERVER = process.env.ARGOCD_SERVER || "argocd-server.argocd.svc.cluster.local:80";

const COMMON_FLAGS = [
  "--server", SERVER,
  "--plaintext",
  "--grpc-web",
  "--grpc-web-root-path", "/argocd",
];

// Refuse mutating subcommands at the MCP layer. RBAC denies them too,
// so this is defense-in-depth — fails fast with a clear message
// instead of an opaque API 403.
const REFUSED_SUBCOMMANDS = new Set([
  "create", "delete", "set", "patch", "edit",
  "repo", "cluster", "proj", "project",
  "cert", "gpg", "admin",
]);
function refuseMutating(args) {
  const first = args[0] ?? "";
  const second = args[1] ?? "";
  if (REFUSED_SUBCOMMANDS.has(first)) return `subcommand 'argocd ${first}' is blocked at the MCP layer (RBAC denies it too).`;
  if (first === "app" && REFUSED_SUBCOMMANDS.has(second)) {
    return `subcommand 'argocd app ${second}' is blocked at the MCP layer.`;
  }
  if (first === "account" && ["update-password", "delete-token"].includes(second)) {
    return `subcommand 'argocd account ${second}' is blocked at the MCP layer.`;
  }
  return null;
}

async function argocd(args, opts = {}) {
  const refusal = refuseMutating(args);
  if (refusal) return { ok: false, stderr: refusal };
  const fullArgs = [...args, ...COMMON_FLAGS];
  try {
    const { stdout, stderr } = await execFileP("argocd", fullArgs, {
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
    content: [{ type: "text", text: `error: ${res.stderr || `argocd exit ${res.code}`}` }],
  };
}

const APP_SCHEMA = {
  type: "string",
  description: "Application name. Must match `openclaw-sandbox-<name>` — ArgoCD RBAC rejects anything else with Forbidden.",
};

const server = new Server(
  { name: "argocd", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "argocd_app_list",
      description:
        "List ArgoCD Applications visible to the bot (the openclaw-sandbox-* set). Returns JSON.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "argocd_app_get",
      description:
        "Fetch one Application's manifest + sync/health status as JSON. Use this to confirm an app exists and read its current state before triggering a sync.",
      inputSchema: {
        type: "object",
        properties: { app: APP_SCHEMA },
        required: ["app"],
      },
    },
    {
      name: "argocd_app_diff",
      description:
        "Show the diff between the app's live state and what its git source would produce on the next sync. Empty diff = nothing to sync (skip the sync call). Pre-sync verification step.",
      inputSchema: {
        type: "object",
        properties: { app: APP_SCHEMA },
        required: ["app"],
      },
    },
    {
      name: "argocd_app_sync",
      description:
        "Trigger a sync (apply git → cluster). Blocks until the sync finishes (success or fail). Set prune=true to also delete cluster resources that no longer exist in git — verify with argocd_app_diff first so the user knows what disappears.",
      inputSchema: {
        type: "object",
        properties: {
          app: APP_SCHEMA,
          prune: { type: "boolean", description: "Also delete resources that have been removed from git. Default false. ALWAYS confirm with the user before using.", default: false },
          dryRun: { type: "boolean", description: "Calculate the sync plan without applying. Default false.", default: false },
        },
        required: ["app"],
      },
    },
    {
      name: "argocd_app_history",
      description: "List past sync revisions for an app, oldest first. Each entry has an ID you can pass to argocd_app_rollback.",
      inputSchema: {
        type: "object",
        properties: { app: APP_SCHEMA },
        required: ["app"],
      },
    },
    {
      name: "argocd_app_rollback",
      description:
        "Re-deploy a previous sync revision by its history ID (from argocd_app_history). Useful when a recent sync broke something and you want to revert without touching git. Same blast radius as argocd_app_sync, ask the user before invoking.",
      inputSchema: {
        type: "object",
        properties: {
          app: APP_SCHEMA,
          id: { type: "integer", description: "History ID from argocd_app_history." },
        },
        required: ["app", "id"],
      },
    },
    {
      name: "argocd_whoami",
      description: "Report which account this token authenticates as. Confirms the bot is using its sandbox-scoped identity.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "argocd_run",
      description:
        "Escape hatch for argocd subcommands not covered by typed tools above. Args are forwarded to argocd CLI verbatim. Mutating subcommands (create / delete / set / patch / edit / repo / cluster / proj / cert / gpg / admin) are blocked at this MCP layer even before argocd-server's RBAC sees them.",
      inputSchema: {
        type: "object",
        properties: {
          args: {
            type: "array",
            items: { type: "string" },
            description: "argv after `argocd`, e.g. ['app', 'logs', 'openclaw-sandbox-foo', '--container', 'nginx'].",
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
    case "argocd_app_list":
      return asText(await argocd(["app", "list", "-o", "json"]));
    case "argocd_app_get":
      return asText(await argocd(["app", "get", a.app, "-o", "json"]));
    case "argocd_app_diff":
      return asText(await argocd(["app", "diff", a.app]));
    case "argocd_app_sync": {
      const args = ["app", "sync", a.app];
      if (a.prune) args.push("--prune");
      if (a.dryRun) args.push("--dry-run");
      return asText(await argocd(args));
    }
    case "argocd_app_history":
      return asText(await argocd(["app", "history", a.app]));
    case "argocd_app_rollback":
      return asText(await argocd(["app", "rollback", a.app, String(a.id)]));
    case "argocd_whoami":
      return asText(await argocd(["account", "get-user-info"]));
    case "argocd_run":
      return asText(await argocd(a.args));
    default:
      return { isError: true, content: [{ type: "text", text: `unknown tool: ${name}` }] };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[argocd-mcp] ready");

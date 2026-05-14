#!/usr/bin/env node
// aws-mcp: thin stdio-MCP wrapper around `aws` (CLI v2). Auth comes
// from the standard chain (env: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
// / AWS_SESSION_TOKEN, or ~/.aws/credentials). The MCP does not manage
// creds and does not switch profiles — `--profile` is rejected at this
// layer so a misbehaving prompt can't roll the bot onto a different
// account.
//
// Tool surface mirrors azure-mcp: identity introspection, regions,
// tagged-resource inventory, policy view, plus an escape-hatch
// aws_run for subcommands not modeled here. Defense-in-depth refusals
// block secret reads (Secrets Manager / SSM SecureString), IAM/KMS
// mutation, and STS AssumeRole regardless of the live IAM policy.
//
// Creds are deliberately NOT wired by the chart in this revision —
// `aws sts get-caller-identity` will fail with "Unable to locate
// credentials" until a human pastes IAM access keys (or wires
// IRSA-style OIDC) into openclaw-secrets. That auth-error path is
// the expected v1 state.

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
  // Secret reads — Secrets Manager & SSM SecureString.
  if (/\bsecretsmanager\b.*\b(get-secret-value|describe-secret)\b/.test(flat))
    return "secretsmanager value access is blocked at the MCP layer.";
  if (/\bssm\b.*\bget-parameters?\b.*\bwithdecryption\b/.test(flat))
    return "ssm SecureString decryption is blocked at the MCP layer.";
  if (/\bkms\b\s+(decrypt|encrypt|re-encrypt|sign|verify|generate-data-key)\b/.test(flat))
    return "kms cryptographic operations are blocked at the MCP layer.";
  // Identity/credential mutation and impersonation.
  if (/\biam\b\s+(create|update|delete|attach|detach|put|tag|untag|add|remove)/.test(flat))
    return "iam mutation is blocked at the MCP layer.";
  if (/\bsts\b\s+(assume-role|assume-role-with-saml|assume-role-with-web-identity|get-federation-token|get-session-token)\b/.test(flat))
    return "sts role-assumption / federation is blocked at the MCP layer.";
  // Profile-switching at MCP layer (creds come from env or ~/.aws only).
  if (flat.includes(" --profile ") || flat.startsWith("--profile "))
    return "--profile is rejected; the MCP uses the default credential chain.";
  return null;
}

async function aws(args, opts = {}) {
  const refusal = refuses(args);
  if (refusal) return { ok: false, stderr: refusal };
  try {
    const { stdout, stderr } = await execFileP("aws", args, {
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
    content: [{ type: "text", text: `error: ${res.stderr || `aws exit ${res.code}`}` }],
  };
}

const server = new Server(
  { name: "aws", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "aws_identity",
      description: "Who am I — STS GetCallerIdentity for the bot's IAM principal (account id + arn + user/role id).",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "aws_regions",
      description: "List regions enabled for this AWS account.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "aws_resources",
      description: "Cross-service inventory via Resource Groups Tagging API. Requires the principal to have tag:GetResources. Optionally filter by tag or resource type.",
      inputSchema: {
        type: "object",
        properties: {
          region: { type: "string", description: "Region (default: from env / config)." },
          resource_type_filters: {
            type: "array", items: { type: "string" },
            description: "Restrict to specific service types, e.g. ['ec2:instance','s3'].",
          },
          tag_key: { type: "string", description: "Filter resources carrying this tag key." },
          tag_values: { type: "array", items: { type: "string" }, description: "Required values for tag_key (any-of)." },
        },
      },
    },
    {
      name: "aws_attached_policies",
      description: "Which IAM policies are attached to the bot's identity. Useful when an API call returns AccessDenied and you need to introspect 'what am I actually allowed to do'.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "aws_run",
      description: "Escape hatch: invoke `aws <args>` with arbitrary arguments. The MCP rejects secret reads, IAM/KMS mutation, sts assume-role, and --profile regardless of the live IAM policy; everything else is gated by IAM only.",
      inputSchema: {
        type: "object",
        properties: {
          args: { type: "array", items: { type: "string" }, description: "argv after the `aws` binary." },
        },
        required: ["args"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: a } = req.params;
  switch (name) {
    case "aws_identity":
      return asText(await aws(["sts", "get-caller-identity", "--output", "json"]));
    case "aws_regions":
      return asText(await aws(["ec2", "describe-regions", "--output", "json"]));
    case "aws_resources": {
      const args = ["resourcegroupstaggingapi", "get-resources", "--output", "json"];
      if (a.region) args.push("--region", a.region);
      if (Array.isArray(a.resource_type_filters) && a.resource_type_filters.length)
        args.push("--resource-type-filters", ...a.resource_type_filters);
      if (a.tag_key) {
        const tagFilter = { Key: a.tag_key };
        if (Array.isArray(a.tag_values) && a.tag_values.length) tagFilter.Values = a.tag_values;
        args.push("--tag-filters", JSON.stringify(tagFilter));
      }
      return asText(await aws(args));
    }
    case "aws_attached_policies": {
      // Resolve identity first to figure out user vs role, then list policies.
      const ident = await aws(["sts", "get-caller-identity", "--output", "json"]);
      if (!ident.ok) return asText(ident);
      let arn = "";
      try { arn = JSON.parse(ident.stdout).Arn ?? ""; } catch {}
      if (/:user\//.test(arn)) {
        const user = arn.split(":user/")[1];
        return asText(await aws(["iam", "list-attached-user-policies", "--user-name", user, "--output", "json"]));
      }
      if (/:assumed-role\//.test(arn)) {
        const roleName = arn.split(":assumed-role/")[1].split("/")[0];
        return asText(await aws(["iam", "list-attached-role-policies", "--role-name", roleName, "--output", "json"]));
      }
      return { content: [{ type: "text", text: `cannot map principal arn to user or role: ${arn}` }] };
    }
    case "aws_run":
      return asText(await aws(a.args));
    default:
      return { isError: true, content: [{ type: "text", text: `unknown tool: ${name}` }] };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[aws-mcp] ready");

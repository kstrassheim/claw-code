#!/usr/bin/env node
// alicloud-mcp: thin stdio-MCP wrapper around `aliyun` (Alibaba Cloud
// CLI). Auth comes from one of two sources:
//   1. AccessKey-style env vars (ALIBABA_CLOUD_ACCESS_KEY_ID +
//      ALIBABA_CLOUD_ACCESS_KEY_SECRET, optional
//      ALIBABA_CLOUD_SECURITY_TOKEN for STS).
//   2. The cached profile at ~/.aliyun/config.json from a prior
//      `aliyun configure` run.
// The MCP does not call `aliyun configure` on behalf of the agent —
// any config-mutation subcommand is refused at this layer.
//
// Tool surface mirrors azure-mcp: identity, regions, cross-service
// resource inventory via the Resource Center service, IAM-equivalent
// policy view, plus an escape-hatch alicloud_run. Defense-in-depth
// rejects KMS crypto, RAM/IAM mutation, and Sec/Secret-manager value
// reads regardless of the live policy.
//
// Creds are NOT wired by the chart in this revision. The CLI will
// return "InvalidAccessKeyId.NotFound" or similar until a RAM-user
// AccessKey is sealed into openclaw-secrets.

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
  if (/\bkms\b\s+(decrypt|encrypt|sign|verify|generatedatakey|reencrypt)/.test(flat))
    return "kms cryptographic operations are blocked at the MCP layer.";
  if (/\bkms\b\s+(createkey|disablekey|enablekey|scheduledeletion|cancelkeydeletion|deletekey|importkeymaterial)/.test(flat))
    return "kms key-mutation is blocked at the MCP layer.";
  if (/\b(ram|resourcemanager)\b\s+(create|update|delete|attach|detach|add|remove|put|set)/.test(flat))
    return "ram / resource-manager identity mutation is blocked at the MCP layer.";
  if (/\bram\s+(getsecretvalue|getsecret)\b/.test(flat))
    return "secret-value access is blocked at the MCP layer.";
  if (/\bkms\s+getsecretvalue\b/.test(flat))
    return "kms secret-value access is blocked at the MCP layer.";
  if (/\bsts\s+assumerole\b/.test(flat))
    return "sts AssumeRole is blocked at the MCP layer.";
  if (/\bconfigure\b/.test(flat))
    return "`aliyun configure` is blocked at the MCP layer; creds come from env or pre-existing profile.";
  return null;
}

async function aliyun(args, opts = {}) {
  const refusal = refuses(args);
  if (refusal) return { ok: false, stderr: refusal };
  try {
    const { stdout, stderr } = await execFileP("aliyun", args, {
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
    content: [{ type: "text", text: `error: ${res.stderr || `aliyun exit ${res.code}`}` }],
  };
}

const server = new Server(
  { name: "alicloud", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "alicloud_identity",
      description: "Who am I — STS GetCallerIdentity for the bot's RAM principal (account id + arn + identity type).",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "alicloud_regions",
      description: "List the Alibaba Cloud regions the account has access to.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "alicloud_resources",
      description: "Cross-service inventory via the Resource Center service (`aliyun resourcecenter SearchResources`). Requires the principal to have `ResourceCenter:Get*` and the service enabled on the account.",
      inputSchema: {
        type: "object",
        properties: {
          region: { type: "string", description: "Region id (e.g. 'cn-hangzhou'). Defaults to the profile's region." },
          resource_type: { type: "string", description: "Restrict to a single resource type, e.g. 'ACS::ECS::Instance'." },
          filter: { type: "string", description: "Additional KV filter (JSON-encoded) passed to --Filter." },
        },
      },
    },
    {
      name: "alicloud_my_ram_policies",
      description: "List RAM policies attached to the bot's user identity. Useful when an API call returns NoPermission and you need to introspect what scopes you actually have.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "alicloud_run",
      description: "Escape hatch: invoke `aliyun <args>` with arbitrary arguments. The MCP rejects KMS crypto, RAM/identity mutation, secret-value access, `aliyun configure`, and STS AssumeRole regardless of the live policy.",
      inputSchema: {
        type: "object",
        properties: {
          args: { type: "array", items: { type: "string" }, description: "argv after the `aliyun` binary, e.g. ['ecs','DescribeInstances','--RegionId','cn-hangzhou']." },
        },
        required: ["args"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: a } = req.params;
  switch (name) {
    case "alicloud_identity":
      return asText(await aliyun(["sts", "GetCallerIdentity"]));
    case "alicloud_regions":
      return asText(await aliyun(["ecs", "DescribeRegions"]));
    case "alicloud_resources": {
      const args = ["resourcecenter", "SearchResources"];
      if (a.region) args.push("--RegionId", a.region);
      if (a.resource_type) args.push("--ResourceType", a.resource_type);
      if (a.filter) args.push("--Filter", a.filter);
      return asText(await aliyun(args));
    }
    case "alicloud_my_ram_policies": {
      // Resolve the active RAM user, then list their attached policies.
      const ident = await aliyun(["sts", "GetCallerIdentity"]);
      if (!ident.ok) return asText(ident);
      let userName = "";
      try {
        const j = JSON.parse(ident.stdout);
        // STS returns the principal arn as "acs:ram::<acct>:user/<name>".
        userName = j?.Arn?.split(":user/")?.[1] ?? "";
      } catch {}
      if (!userName) return { content: [{ type: "text", text: `cannot derive RAM user from identity output: ${ident.stdout}` }] };
      return asText(await aliyun(["ram", "ListPoliciesForUser", "--UserName", userName]));
    }
    case "alicloud_run":
      return asText(await aliyun(a.args));
    default:
      return { isError: true, content: [{ type: "text", text: `unknown tool: ${name}` }] };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[alicloud-mcp] ready");

#!/usr/bin/env node
// k8s-mcp: thin stdio-MCP wrapper around `kubectl`, scoped to whatever
// the kubeconfig at ~/.kube/config grants. Same RBAC as the underlying
// SA token — if the API server rejects something the MCP tool surfaces
// the rejection unchanged; no privilege escalation happens here.
//
// Tools chosen to cover the common dev loop (list / get / logs / apply
// / delete / describe / exec / scale). Anything more exotic the agent
// still has direct `kubectl` access for.
//
// Why a thin wrapper instead of the upstream Flux159/mcp-server-kubernetes:
// predictable build (no external npm dep churn), scope-matched tool set,
// consistent with the gmail-mcp + entra-totp pattern already in the
// image. Maintenance cost is small — kubectl's CLI is stable.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileP = promisify(execFile);

async function kubectl(args, opts = {}) {
  try {
    const { stdout, stderr } = await execFileP("kubectl", args, {
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
  // Standard envelope for the agent: stdout if successful, otherwise
  // the kubectl error message as a clear "error: ..." prefix. We never
  // swallow the API-server error text — the agent's reasoning about
  // RBAC denials depends on seeing them verbatim.
  if (res.ok) return { content: [{ type: "text", text: res.stdout || "(no output)" }] };
  return {
    isError: true,
    content: [{ type: "text", text: `error: ${res.stderr || `kubectl exit ${res.code}`}` }],
  };
}

const NS_SCHEMA = {
  type: "string",
  description: "Namespace. Required for namespaced resources; the agent's RBAC will reject any namespace outside its sandbox.",
};

const server = new Server(
  { name: "k8s", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "k8s_list",
      description:
        "List resources of a given type in a namespace. Returns one row per item with name + key columns. Use this instead of shelling `kubectl get`.",
      inputSchema: {
        type: "object",
        properties: {
          resource: { type: "string", description: "Resource kind, plural: pods, deployments, services, configmaps, ingresses, jobs, etc." },
          namespace: NS_SCHEMA,
          selector: { type: "string", description: "Optional label selector (kubectl -l), e.g. 'app=nginx'." },
        },
        required: ["resource", "namespace"],
      },
    },
    {
      name: "k8s_get",
      description:
        "Get one resource as YAML. Includes status (.status) so a single call covers `describe`-style introspection too.",
      inputSchema: {
        type: "object",
        properties: {
          resource: { type: "string" },
          name: { type: "string" },
          namespace: NS_SCHEMA,
        },
        required: ["resource", "name", "namespace"],
      },
    },
    {
      name: "k8s_logs",
      description: "Tail logs from a pod (defaults to last 100 lines).",
      inputSchema: {
        type: "object",
        properties: {
          pod: { type: "string", description: "Pod name, or 'deploy/<name>' / 'job/<name>' shortcut." },
          namespace: NS_SCHEMA,
          container: { type: "string", description: "Container name when the pod has multiple." },
          lines: { type: "integer", description: "Tail line count, default 100, max 1000.", default: 100 },
          previous: { type: "boolean", description: "Read the previous-instance logs (after a crash-restart). Default false.", default: false },
        },
        required: ["pod", "namespace"],
      },
    },
    {
      name: "k8s_apply",
      description:
        "Apply a manifest (YAML, may be multi-doc). The namespace embedded in each doc must match the agent's sandbox; anything else is rejected by the API server.",
      inputSchema: {
        type: "object",
        properties: {
          manifest: { type: "string", description: "Full YAML content. Multiple --- separated docs are supported." },
        },
        required: ["manifest"],
      },
    },
    {
      name: "k8s_delete",
      description: "Delete one resource by name.",
      inputSchema: {
        type: "object",
        properties: {
          resource: { type: "string" },
          name: { type: "string" },
          namespace: NS_SCHEMA,
        },
        required: ["resource", "name", "namespace"],
      },
    },
    {
      name: "k8s_scale",
      description: "Scale a deployment / replicaset / statefulset to N replicas.",
      inputSchema: {
        type: "object",
        properties: {
          resource: { type: "string", description: "deployment | statefulset | replicaset (or shorthand: deploy, sts, rs)." },
          name: { type: "string" },
          namespace: NS_SCHEMA,
          replicas: { type: "integer", minimum: 0 },
        },
        required: ["resource", "name", "namespace", "replicas"],
      },
    },
    {
      name: "k8s_describe",
      description: "Kubectl-describe-style human-readable summary including events. Useful for triage when a pod is in CrashLoop / Pending.",
      inputSchema: {
        type: "object",
        properties: {
          resource: { type: "string" },
          name: { type: "string" },
          namespace: NS_SCHEMA,
        },
        required: ["resource", "name", "namespace"],
      },
    },
    {
      name: "k8s_exec",
      description: "Run a one-shot command inside a pod. No interactive shell — use for `cat /etc/hostname`, `nslookup ...`, etc. Long-running commands time out.",
      inputSchema: {
        type: "object",
        properties: {
          pod: { type: "string" },
          namespace: NS_SCHEMA,
          container: { type: "string" },
          command: { type: "array", items: { type: "string" }, description: "argv array, e.g. ['sh','-c','env | grep FOO']" },
          timeoutSeconds: { type: "integer", default: 15, minimum: 1, maximum: 60 },
        },
        required: ["pod", "namespace", "command"],
      },
    },
    {
      name: "k8s_whoami",
      description: "Report the SA identity the kubeconfig is bound to (handy when the agent isn't sure why something is Forbidden).",
      inputSchema: { type: "object", properties: {} },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: a } = req.params;
  switch (name) {
    case "k8s_list": {
      const args = ["get", a.resource, "-n", a.namespace, "-o", "wide"];
      if (a.selector) args.push("-l", a.selector);
      return asText(await kubectl(args));
    }
    case "k8s_get": {
      return asText(await kubectl(["get", a.resource, a.name, "-n", a.namespace, "-o", "yaml"]));
    }
    case "k8s_logs": {
      const lines = Math.min(Math.max(1, a.lines ?? 100), 1000);
      const args = ["logs", a.pod, "-n", a.namespace, `--tail=${lines}`];
      if (a.container) args.push("-c", a.container);
      if (a.previous) args.push("-p");
      return asText(await kubectl(args));
    }
    case "k8s_apply": {
      return asText(await kubectl(["apply", "-f", "-"], { input: a.manifest }));
    }
    case "k8s_delete": {
      return asText(await kubectl(["delete", a.resource, a.name, "-n", a.namespace]));
    }
    case "k8s_scale": {
      return asText(await kubectl([
        "scale", a.resource, a.name, "-n", a.namespace, `--replicas=${a.replicas}`,
      ]));
    }
    case "k8s_describe": {
      return asText(await kubectl(["describe", a.resource, a.name, "-n", a.namespace]));
    }
    case "k8s_exec": {
      const timeout = Math.min(Math.max(1, a.timeoutSeconds ?? 15), 60);
      const args = ["exec", a.pod, "-n", a.namespace];
      if (a.container) args.push("-c", a.container);
      args.push("--");
      args.push(...a.command);
      return asText(await kubectl(args, { timeout: timeout * 1000 }));
    }
    case "k8s_whoami": {
      return asText(await kubectl(["auth", "whoami"]));
    }
    default:
      return { isError: true, content: [{ type: "text", text: `unknown tool: ${name}` }] };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[k8s-mcp] ready");

<!--
  Appended to TOOLS.md ONLY for the openclaw instance (not olga) via
  the conditional in templates/tools-md-configmap.yaml. Describes the
  argocd CLI capabilities (read-only on all apps + sync on
  openclaw-sandbox-* apps).
-->

---

# Argo CD — sandbox-scoped sync access

You have:
- an `argocd` CLI in your environment + auth via `$ARGOCD_AUTH_TOKEN`
  / `$ARGOCD_SERVER` (sealed into your secret env)
- an `argocd` MCP server (registered as `mcp.servers.argocd`) with
  typed tools `argocd_app_list`, `argocd_app_get`, `argocd_app_diff`,
  `argocd_app_sync`, `argocd_app_history`, `argocd_app_rollback`,
  `argocd_whoami`, plus an `argocd_run` escape hatch

Prefer the MCP tools for the common loop (less context burned on
parsing CLI output). Fall back to raw `argocd` shell for advanced
commands not exposed via MCP. Identity is the `openclaw-dev` local
account — **do not** prompt the user for an argocd login,
credentials are injected.

## What you CAN do

- `argocd app get default/openclaw-sandbox-<name>` — read manifest +
  sync status of any sandbox app you have access to
- `argocd app sync default/openclaw-sandbox-<name>` — trigger sync
  for a sandbox app (manifests in git → applied to cluster)
- `argocd app history default/openclaw-sandbox-<name>` — see past
  syncs
- `argocd app rollback default/openclaw-sandbox-<name> <id>` —
  redeploy a previous revision (counts as `action/*`)
- `argocd app diff default/openclaw-sandbox-<name>` — see what would
  change if you synced now
- `argocd repo list` — list configured repositories (read-only)

## What you CANNOT do (and shouldn't try)

- **Create new Applications.** The `create` verb is denied. New
  sandboxes are admin-managed: the user creates an ArgoCD App
  manifest in a gitops repo and applies it; only then can you sync
  it.
- **Delete Applications.** Same — admins clean up old sandboxes.
- **Edit Applications.** No `update` permission. If you want to
  change what an app deploys, change the gitops repo + commit + push
  and let the app pick up the new manifests.
- **See Applications outside `default/openclaw-sandbox-*`.** Apps
  like `openclaw`, `openclaw-infra`, `immich`, `core-ingress` —
  return Forbidden. Don't try to work around this.
- **Manage clusters, projects, accounts, certs, repos beyond
  read.** All denied. The argocd CLI calls will surface 403s.
- **Use `argocd login` / `argocd account update-password`.** The
  account is apiKey-only.

## Workflow rules

1. **Read before sync.** Always `argocd app diff` before `argocd
   app sync` so you (and the user via you) know what's about to
   change. If the diff is empty, the sync is a no-op — skip it.
2. **Sync is bot-initiated, but the user owns the gitops state.**
   You can sync to align cluster ↔ repo, you can't add new
   manifests to the cluster without going through git first.
3. **No `--prune` without confirmation.** `argocd app sync --prune`
   deletes resources that no longer exist in git. If the user asks
   "make the cluster match git", verify which resources would be
   removed via `argocd app diff` first, surface the list to the
   user, and only then `--prune`.
4. **Use `--dry-run` for verification.** Many argocd subcommands
   take it.

## Coordination with kubectl

You have BOTH `kubectl` (sandbox-scoped CRUD) and `argocd`
(sandbox-scoped sync) for the same namespaces. Choose:

- **`argocd sync`** when the source of truth is git (the user
  committed something, you align cluster).
- **`kubectl apply`** for **scratch experimentation** where you
  don't want git to know — e.g. trying out a config quickly, doing
  a debug pod.

If you `kubectl apply` something to a sandbox that's argocd-managed,
the next `argocd app sync` (with or without `--prune`) will overwrite
your changes. That's normal — argocd is the gitops authority.

## Quick reference

| Goal                                       | Command                                                                |
|--------------------------------------------|------------------------------------------------------------------------|
| List the sandbox apps you can see          | `argocd app list`                                                      |
| Inspect one sandbox app                    | `argocd app get default/openclaw-sandbox-<name>`                       |
| Preview what a sync would do               | `argocd app diff default/openclaw-sandbox-<name>`                      |
| Sync (apply from git to cluster)           | `argocd app sync default/openclaw-sandbox-<name>`                      |
| Sync + delete resources gone from git      | `argocd app sync default/openclaw-sandbox-<name> --prune` (confirm first) |
| Roll back to a prior revision              | `argocd app rollback default/openclaw-sandbox-<name> <history-id>`     |
| Refresh sync status                        | `argocd app sync default/openclaw-sandbox-<name> --refresh`            |
| Who am I (token identity)                  | `argocd account get-user-info`                                         |

<!--
  Describes the `gcloud` CLI + gcp-mcp wired into this image.
  Mirrors TOOLS-aws.md / TOOLS-alicloud.md in shape.
-->

---

# GCP — `gcloud` CLI + `mcp.servers.gcp`

## ⚠️ State: binaries installed, credentials NOT wired (yet)

The image ships **`gcloud`** (Google Cloud SDK, pinned via
`GCLOUD_VERSION` in the repo's `VERSIONS` file) plus a thin
**`mcp.servers.gcp`** wrapper. **No credentials are sealed into the
pod in this revision** — every `gcloud ...` call will fail with
`Reauthentication required` / `You do not currently have an active
account selected` until a human seals real bot credentials into
`openclaw-secrets`.

When that happens, the chart will mount one of:

| Approach                | What gets sealed                                                          |
|-------------------------|---------------------------------------------------------------------------|
| Service-account JSON    | `GOOGLE_APPLICATION_CREDENTIALS` pointing at a SA JSON on the PVC         |
| End-user token cache    | `~/.config/gcloud/` (subPath under the workspace PVC) populated externally |
| Workload Identity Fed   | `GOOGLE_APPLICATION_CREDENTIALS` pointing at an external-account JSON     |

The MCP **refuses `gcloud auth login` / `auth revoke` / `auth
print-*-token`** at this layer — you don't run interactive login
flows from inside the bot, the human provisions creds and seals them
into the secret.

Until creds land, **don't pretend the bot can act on GCP.** If a
user asks for a GCP operation, surface the unauthenticated state
plainly and ask them to create a bot service account with the
scope they want.

## Capability boundaries (once creds land)

Whatever your IAM bindings grant you. MCP-layer defense-in-depth:

- ❌ Secret Manager value access (`secrets versions access`)
- ❌ Secret Manager mutation (`secrets create / update / delete /
  add-iam-policy-binding / set-iam-policy`)
- ❌ KMS crypto / key mutation (`kms keys/keyrings ... encrypt /
  decrypt / sign / destroy / import / create / update`)
- ❌ IAM mutation (`iam service-accounts / members / policy-bindings
  create / update / delete / add / remove / set`)
- ❌ `auth login` / `auth application-default login` / `auth revoke`
  / `auth print-access-token` / `auth print-identity-token`
- ❌ `--impersonate-service-account` overrides
- ✅ Everything else your IAM bindings permit

## MCP tool surface (`mcp.servers.gcp.*`)

| Tool                    | What it does                                                              |
|-------------------------|---------------------------------------------------------------------------|
| `gcp_account`           | Composite of `gcloud config list` + `gcloud auth list` — who am I, what's the active project, which accounts are credentialed. |
| `gcp_projects`          | `projects list` — projects visible to your identity.                      |
| `gcp_resources`         | Cross-service inventory via Cloud Asset API (`asset search-all-resources`). Needs the Cloud Asset API enabled and `cloudasset.assets.searchAllResources`. Scope = project / folder / org. |
| `gcp_my_iam_bindings`   | The IAM roles bound to your identity on a given project — answers "what am I allowed".  |
| `gcp_run`               | Escape hatch: `gcloud <args>`, with the refusal patterns above enforced.  |

## When to shell `gcloud` directly

- Streaming logs: `gcloud logging tail`, `gcloud builds log --stream`.
- One-off `--format='value(...)'` projections piped into shell.
- gsutil (also installed) for bulk Cloud Storage operations.

The MCP refusals do NOT apply to shell calls — the human IAM
binding is the only guard.

## Quick reference

| Goal                                     | Command                                                              |
|------------------------------------------|----------------------------------------------------------------------|
| Who am I                                 | `gcloud auth list`                                                   |
| Set active project                       | `gcloud config set project <PROJECT_ID>`                             |
| List compute instances                   | `gcloud compute instances list --format=json`                        |
| List GCS buckets                         | `gsutil ls` or `gcloud storage buckets list`                         |
| List Cloud Run services                  | `gcloud run services list --format=json`                             |
| Find docs for a command                  | `gcloud <topic> --help` or [cloud.google.com/sdk/gcloud/reference](https://cloud.google.com/sdk/gcloud/reference) |

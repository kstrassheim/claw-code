<!--
  Describes the terraform CLI + HashiCorp terraform-mcp-server
  capabilities wired into this image.
-->

---

# Terraform — IaC for the bot's scope

You have:
- **`terraform`** CLI (HashiCorp, pinned via `TERRAFORM_VERSION` in
  the repo's `VERSIONS` file). Standard subcommands work: `init`,
  `plan`, `apply`, `destroy`, `validate`, `fmt`, `show`,
  `state list / show`, `output`.
- **`mcp.servers.terraform`** — HashiCorp's official
  `terraform-mcp-server` registered as a stdio MCP. Tools include
  Registry doc / module lookups (so you don't have to scrape
  registry.terraform.io HTML to learn a provider's resource
  schema), and depending on the upstream version, plan/apply
  helpers. Check the live `listTools` for current surface.

Providers you can talk to depend on which credentials the operator
has sealed into `openclaw-secrets`:

| Provider     | How auth works                                                                                                  |
|--------------|------------------------------------------------------------------------------------------------------------------|
| `azurerm`    | Reads `~/.azure/` token cache when an Entra bot user is configured (see TOOLS-entra.md). Otherwise unauthenticated. |
| `kubernetes` | Reads `~/.kube/config` (or the in-cluster ServiceAccount token, narrow RBAC by default — see TOOLS-k8s.md).      |
| `helm`       | Reads `~/.kube/config`.                                                                                          |
| `github`     | Reads `$GITHUB_TOKEN` (bot's classic PAT, see TOOLS-github.md).                                                  |
| `azuread`    | Same Entra cache. Permissions limited to what the bot's Entra role grants.                                       |
| `aws` / `google` / `alicloud` | Each picks up its CLI's credentials when those are wired (TOOLS-aws.md / TOOLS-gcp.md / TOOLS-alicloud.md). |

## Capability boundaries

Your Terraform reach is **exactly what the underlying credentials
grant**. There is no extra MCP-layer enforcement here — the cloud
providers' IAM policies are the only guard. `plan` and `apply` will
succeed only for things those roles permit:

- ✅ Data sources for any provider you have read on
- ❌ Mutations against providers you only have Reader on
- ❌ Reading or writing k8s Secrets when RBAC denies it (terraform's
  `kubernetes_secret` data source returns Forbidden)

If a `plan` shows resources outside what you can touch, surface
that to the user — say which scope is blocking and let them decide
whether to escalate the bot's role or shift to manual ops.

## State storage

Terraform state contains sensitive data (resource IDs, occasionally
plaintext attributes if a provider returns them). The bot's default
RBAC denies writing k8s Secrets, so the `kubernetes` backend won't
work out of the box. Three viable patterns:

1. **Local file state** (`terraform { backend "local" {} }`) under
   `~/.openclaw/workspace/<project>/`. PVC-backed (the workspace
   volume is mounted there). Survives pod restarts. Single-writer
   (you), fine for sandbox experiments.

2. **Inline state in a sandbox ConfigMap**: store the state file
   in a `ConfigMap` you `kubectl create` in a sandbox namespace
   you have write access to. Less secure (ConfigMaps are not
   encrypted at rest), but auditable. Don't use for anything
   beyond playground.

3. **External state backend the user sets up**: Azure storage
   account / S3 / GCS / Terraform Cloud. Requires creds the user
   must provide. Don't try to wire this yourself.

Default to (1) unless the user specifies otherwise.

## Workflow rules

1. **`terraform plan` before `terraform apply` — always.** The plan
   output is what the user reviews before you mutate anything.
   `apply` without a recently-saved plan is opaque; never run
   `apply` on autopilot.

2. **`-target` is a code smell.** Don't reach for `terraform apply
   -target=<resource>` to work around plan errors. Fix the
   underlying config or surface the issue to the user.

3. **`-auto-approve` only with explicit user OK.** Default flow is
   `terraform apply` → prompt for confirmation. You can pipe `yes`
   if the user said "go ahead and apply", not otherwise.

4. **Module sources are URLs you should pin.** Reference modules
   by registry path with a version pin (`source = "..."`,
   `version = "x.y.z"`). Floating versions break determinism.

5. **`terraform destroy` is a separate confirmation.** Even more
   than `apply` — never destroy without the user reading and
   acknowledging the plan.

6. **State files are output-on-error.** When something errors, the
   state-mutation may have partially applied. Save the state file
   somewhere (PVC) before retrying, so the user has a recovery
   point.

## Quick reference

| Goal                                           | Command                                                   |
|------------------------------------------------|-----------------------------------------------------------|
| Init a new project                             | `terraform init`                                          |
| Validate config syntax                         | `terraform validate`                                      |
| See what would change                          | `terraform plan -out=tfplan`                              |
| Apply (the just-planned changes)               | `terraform apply tfplan`                                  |
| Apply with confirmation skipped (with user OK) | `terraform apply -auto-approve`                           |
| Show current state                             | `terraform state list` + `terraform state show <addr>`    |
| Get a single output                            | `terraform output <name>`                                 |
| Find docs for a provider's resource            | use `mcp.servers.terraform` registry-lookup tools         |
| Destroy everything (with user OK)              | `terraform destroy -auto-approve`                         |

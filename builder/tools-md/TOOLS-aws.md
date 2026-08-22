<!--
  Describes the AWS CLI v2 + aws-mcp wired into this image.
  Mirrors TOOLS-gcp.md / TOOLS-alicloud.md / TOOLS-entra.md in shape.
-->

---

# AWS — `aws` CLI v2 + `mcp.servers.aws`

## ⚠️ State: binaries installed, credentials NOT wired (yet)

The image ships **`aws` CLI v2** (pinned via `AWS_CLI_VERSION` in
the repo's `VERSIONS` file) plus a thin **`mcp.servers.aws`**
wrapper. **No credentials are sealed into the pod in this revision**
— every `aws ...` call will fail with `Unable to locate credentials`
until a human seals real IAM access keys into `openclaw-secrets`.

When that happens, the chart will mount:

| Env var                     | Purpose                                          |
|-----------------------------|--------------------------------------------------|
| `AWS_ACCESS_KEY_ID`         | IAM access key id (bot IAM user)                 |
| `AWS_SECRET_ACCESS_KEY`     | IAM secret access key                            |
| `AWS_DEFAULT_REGION`        | Default region for region-implicit commands      |
| (optional) `AWS_SESSION_TOKEN` | Set only for STS-issued temp credentials      |

Until then, **don't pretend the bot can act on AWS.** If a user asks
for an AWS operation, surface the unauthenticated state plainly and
ask them to provision a bot IAM user with the scope they want.

## Capability boundaries (once creds land)

Whatever IAM grants you, you have — nothing more. The MCP layer
adds **defense-in-depth** that holds even if the IAM policy is
broader than intended:

- ❌ Secrets Manager `get-secret-value` / `describe-secret`
- ❌ SSM Parameter Store `get-parameters --with-decryption`
- ❌ KMS `encrypt` / `decrypt` / `sign` / `verify` / `generate-data-key`
- ❌ IAM mutation (`create-*`, `update-*`, `delete-*`, `attach-*`,
  `detach-*`, `put-*`, `tag-*`)
- ❌ `sts assume-role*` / `get-federation-token` / `get-session-token`
- ❌ `--profile` overrides (you use the default credential chain only)
- ✅ Everything else IAM permits

If a `plan` / `apply` from terraform shows resources you can't touch,
surface that to the user — say which IAM action is blocking and let
them decide whether to escalate the bot's role.

## MCP tool surface (`mcp.servers.aws.*`)

| Tool                       | What it does                                                              |
|----------------------------|---------------------------------------------------------------------------|
| `aws_identity`             | `sts get-caller-identity` — account id, arn, identity type.               |
| `aws_regions`              | Regions enabled for this account.                                         |
| `aws_resources`            | Cross-service inventory via Resource Groups Tagging API (needs `tag:GetResources`). Optionally filter by service or tag. |
| `aws_attached_policies`    | The IAM policies attached to your identity — answers "what am I allowed". |
| `aws_run`                  | Escape hatch: `aws <args>`, with the refusal patterns above enforced.     |

## When to shell `aws` directly

The MCP is enough for inspection and most everyday calls; reach for
the shell when:

- You need streaming output (`aws logs tail`, `aws cloudformation deploy`).
- You're piping into `jq` for a quick aggregation.
- You're using a subcommand that takes `--cli-input-json` from a file
  — the MCP's escape hatch supports it, but argv reads cleaner from
  the shell.

The same refusals do NOT apply at the shell — be deliberate about
secret reads and IAM mutation. The IAM policy is the only guard.

## Quick reference

| Goal                                     | Command                                                              |
|------------------------------------------|----------------------------------------------------------------------|
| Who am I                                 | `aws sts get-caller-identity`                                        |
| List EC2 instances in a region           | `aws ec2 describe-instances --region us-east-1`                      |
| List S3 buckets                          | `aws s3api list-buckets`                                             |
| List Lambdas                             | `aws lambda list-functions`                                          |
| List my policies                         | `aws iam list-attached-user-policies --user-name <self>`             |
| Find docs for a service                  | `aws <service> help` or [docs.aws.amazon.com/cli](https://docs.aws.amazon.com/cli/) |

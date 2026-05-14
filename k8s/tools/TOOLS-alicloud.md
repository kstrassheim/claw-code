<!--
  Describes the `aliyun` CLI + alicloud-mcp wired into this image.
  Mirrors TOOLS-aws.md / TOOLS-gcp.md in shape.
-->

---

# Alibaba Cloud — `aliyun` CLI + `mcp.servers.alicloud`

## ⚠️ State: binaries installed, credentials NOT wired (yet)

The image ships **`aliyun`** (Alibaba Cloud CLI, pinned via
`ALIYUN_CLI_VERSION` in the repo's `VERSIONS` file) plus a thin
**`mcp.servers.alicloud`** wrapper. **No credentials are sealed
into the pod in this revision** — every `aliyun ...` call will fail
with `InvalidAccessKeyId.NotFound` / `NoActiveProfile` until a human
seals real RAM-user AccessKeys into `openclaw-secrets`.

When that happens, the chart will mount:

| Env var                                | Purpose                                       |
|----------------------------------------|-----------------------------------------------|
| `ALIBABA_CLOUD_ACCESS_KEY_ID`          | RAM user AccessKey id                         |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET`      | RAM user AccessKey secret                     |
| (optional) `ALIBABA_CLOUD_SECURITY_TOKEN` | Set only for STS-issued temp credentials   |
| (optional) `ALIBABA_CLOUD_REGION_ID`   | Default region (e.g. `cn-hangzhou`)           |

The MCP **refuses `aliyun configure`** at this layer — credential
management isn't a runtime operation for the bot, it's a deploy-time
seal.

Until creds land, **don't pretend the bot can act on Alibaba Cloud.**

## Capability boundaries (once creds land)

Whatever the RAM policy grants you. MCP-layer defense-in-depth:

- ❌ KMS crypto (`Decrypt` / `Encrypt` / `Sign` / `Verify` /
  `GenerateDataKey` / `Reencrypt`)
- ❌ KMS key mutation (`CreateKey` / `DisableKey` / `EnableKey` /
  `ScheduleDeletion` / `CancelKeyDeletion` / `DeleteKey` /
  `ImportKeyMaterial`)
- ❌ RAM / Resource Manager identity mutation (`Create*` / `Update*`
  / `Delete*` / `Attach*` / `Detach*` / `Add*` / `Remove*` / `Put*` /
  `Set*`)
- ❌ Secret value reads (`GetSecretValue` / `GetSecret` under either
  `Ram` or `Kms` service)
- ❌ `sts AssumeRole`
- ❌ `aliyun configure` (any subcommand)
- ✅ Everything else your RAM policy permits

## MCP tool surface (`mcp.servers.alicloud.*`)

| Tool                          | What it does                                                                 |
|-------------------------------|------------------------------------------------------------------------------|
| `alicloud_identity`           | `sts GetCallerIdentity` — account id, arn, identity type.                    |
| `alicloud_regions`            | `ecs DescribeRegions` — regions accessible to the account.                   |
| `alicloud_resources`          | Cross-service inventory via `resourcecenter SearchResources`. Service must be enabled and principal needs `ResourceCenter:Get*`. |
| `alicloud_my_ram_policies`    | `ram ListPoliciesForUser` for the active identity — answers "what am I allowed". |
| `alicloud_run`                | Escape hatch: `aliyun <args>`, with the refusal patterns above enforced.     |

## Aliyun CLI shape primer

Commands follow `aliyun <Product> <Action> [--ParamName Value]`. The
PascalCase product/action names match the underlying OpenAPI:

```
aliyun ecs DescribeInstances --RegionId cn-hangzhou
aliyun oss ls oss://my-bucket/
aliyun rds DescribeDBInstances --RegionId cn-shanghai
aliyun sts GetCallerIdentity
```

`--RegionId` is per-call; `--profile` switches the active named
profile in `~/.aliyun/config.json` (which the MCP refuses anyway —
use env vars).

## When to shell `aliyun` directly

- One-off API calls to services not modeled by the typed tools.
- Bulk `oss` operations (analog of `aws s3 cp`).
- Piping JSON output through `jq` for ad-hoc projections.

The MCP refusals do NOT apply to shell calls — the RAM policy is
the only guard.

## Quick reference

| Goal                                     | Command                                                              |
|------------------------------------------|----------------------------------------------------------------------|
| Who am I                                 | `aliyun sts GetCallerIdentity`                                       |
| List ECS instances                       | `aliyun ecs DescribeInstances --RegionId cn-hangzhou`                |
| List OSS buckets                         | `aliyun oss ls`                                                      |
| List RDS instances                       | `aliyun rds DescribeDBInstances --RegionId cn-hangzhou`              |
| List my RAM policies                     | `aliyun ram ListPoliciesForUser --UserName <self>`                   |
| Find docs                                | `aliyun <Product> --help` or [help.aliyun.com](https://help.aliyun.com) |

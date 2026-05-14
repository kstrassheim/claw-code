# claw-code

Autonomous coding agent powered by OpenClaw, deployed on AKS.

## Architecture

```
claw-code/
├── main.tf                  # Terraform: AKS cluster, PV storage, Kyverno, Sealed Secrets
├── variables.tf             # Terraform variables
├── terraform.tfvars          # Dev environment values
├── builder/
│   ├── Dockerfile            # Custom openclaw image (all coding features, no astrology)
│   ├── k8s-mcp/             # kubectl MCP
│   ├── azure-mcp/           # az CLI MCP
│   ├── aws-mcp/             # AWS CLI MCP
│   ├── gcp-mcp/             # gcloud MCP
│   ├── alicloud-mcp/        # aliyun CLI MCP
│   ├── debug-mcp/           # Node CDP debugger MCP
│   └── entra-totp/          # TOTP generator for Entra MFA
├── k8s/
│   ├── 000-namespace-and-config.yaml  # Namespace, ConfigMap (openclaw tools-md)
│   ├── 005-pvc.yaml          # PVC for openclaw workspace
│   ├── 010-secrets.yaml     # Secrets (placeholder values; seal for production)
│   ├── 010-openclaw-config.yaml  # ConfigMap: openclaw.json with Mistral Large + optional MiniMax
│   ├── 020-deployment.yaml  # Deployment
│   ├── 030-service.yaml     # ClusterIP service
│   └── 040-networkpolicy.yaml  # default-deny + allow-dns
└── .github/workflows/
    ├── terraform.yml         # PR check + merge-to-main apply pipeline
    ├── build-image.yml       # Build and push custom openclaw image
    ├── deploy-k8s.yml        # kubectl apply -f k8s/ on push to main
    └── seal-secrets.yml      # Seal GH secrets → kubectl apply SealedSecret
```

---

## Prerequisites

### Azure (one-time human setup)

1. **Resource Group** `claw-code` must exist:
   ```bash
   az group create --name claw-code --location westeurope
   ```

2. **User-Assigned Managed Identity** for GitHub Actions OIDC:
   ```bash
   az identity create --name github-claw-code --resource-group claw-code
   ```
   Note the `clientId` for `AZURE_CLIENT_ID`.

3. **Federated Credentials** on the identity:
   - Azure Portal → Entra ID → App registrations → find your MI → Certificates & secrets → Federated credentials
   - Add credential:
     - **Trusted entity**: GitHub Actions
     - **Organization**: `kstrassheim`
     - **Repository**: `claw-code`
     - **Environment**: `dev` (or `All environments`)
     - **Audience**: `api://AzureADTokenExchange`

4. **Role assignments** on the identity:
   ```bash
   MI_PRINCIPAL_ID=$(az identity show --name github-claw-code --resource-group claw-code --query principalId -o tsv)
   RG_ID=$(az group show --name claw-code --query id -o tsv)

   az role assignment create --role "Contributor" --assignee-object-id $MI_PRINCIPAL_ID --scope $RG_ID
   az role assignment create --role "Storage Account Contributor" --assignee-object-id $MI_PRINCIPAL_ID --scope $RG_ID
   ```

5. **Terraform state** already exists:
   - Account: `mytofustates`
   - Container: `claw-code`
   - Key: `dev.tfstate`

### GitHub Secrets

Set in `Settings → Secrets and variables → Actions`:

| Secret | Required | Description |
|--------|----------|-------------|
| `AZURE_CLIENT_ID` | Yes | Client ID of the deploy-managed identity |
| `AZURE_TENANT_ID` | Yes | Entra tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Subscription ID |
| `MISTRAL_API_KEY` | Yes | Mistral Large API key |
| `GITHUB_TOKEN` | Yes | Bot account PAT (with repo access) |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token for bot communication |
| `MINIMAX_API_KEY` | No | Optional — if present, MiniMax appears in model list |

---

## Stage 1: Terraform (Infrastructure)

### What gets deployed

| Resource | Purpose |
|---|---|
| AKS cluster (1x Standard_D2s_v3, 8GB RAM, 2vCPU) | K8s cluster |
| Azure Storage Account | PV (Azure Files) for openclaw workspace |
| Azure File Share (`openclaw`, 50GB) | K8s PersistentVolume |
| Kyverno (v3.3.2) | Policy engine, default-deny-all |
| Sealed Secrets (v2.16.2) | kubeseal-compatible controller in kube-system |
| Log Analytics Workspace | AKS monitoring |

### Terraform workflow

- **PR to `main`**: Runs `terraform plan` — posts a summary comment to the PR.
- **Merge to `main`**: Runs `terraform apply` automatically.

### Terraform variables

| Variable | Default | Description |
|---|---|---|
| `env` | `dev` | Environment name |
| `cluster_name` | `claw-code-aks` | AKS cluster name |
| `location` | `westeurope` | Azure region |
| `node_size` | `Standard_D2s_v3` | VM size (2 vCPU, 8GB RAM) |
| `node_count` | `1` | Number of nodes |
| `storage_account_name` | `clwcodecodev` | Globally unique storage account name |
| `namespace` | `openclaw` | K8s namespace |

---

## Stage 2: Custom Docker Image

### What's included

✅ VSCode (code-server)
✅ Azure CLI + azure-mcp
✅ AWS CLI v2 + aws-mcp
✅ Google Cloud CLI + gcp-mcp
✅ Alibaba Cloud CLI + alicloud-mcp
✅ kubectl + k8s-mcp
✅ Terraform CLI + terraform-mcp-server
✅ GitHub CLI + github-mcp-server
✅ Chromium (browser automation for Entra login)
✅ podman (rootless, vfs storage)
✅ code-server (full VSCode-in-the-browser)
✅ debug-mcp (Node CDP debugger)

### Building the image

```bash
# Build locally (on a Linux arm64 machine with podman)
cd builder/
podman build -t mainpi.local:30500/openclaw/claw-code:<tag> \
  --build-arg BASE_IMAGE=mainpi.local:30500/openclaw/openclaw:local .
podman push mainpi.local:30500/openclaw/claw-code:<tag>

# Then update k8s/020-deployment.yaml image tag, commit, and push to main.
# The deploy-k8s workflow applies the manifest to the cluster.
```

Or trigger the `build-image.yml` workflow via GitHub Actions (workflow_dispatch).

### Updating the image tag

After building a new image, update the `image:` section in `k8s/020-deployment.yaml`:
```yaml
image: mainpi.local:30500/openclaw/claw-code:<NEW_TAG>
```
Commit and push to main — the `deploy-k8s` workflow applies the change to the cluster.

---

## LLMs

- **Default**: Mistral Large (`mistral-large-latest`)
- **Optional**: MiniMax (`MiniMax/MiniMax-Text-01`) — activated only when `MINIMAX_API_KEY` is present in `k8s/010-secrets.yaml`. When absent, MiniMax does not appear in the model list.

To activate MiniMax:
1. Add `MINIMAX_API_KEY: "<your-key>"` in `k8s/010-secrets.yaml`
2. Commit and push to main — `deploy-k8s` applies the change, pod restarts, MiniMax appears.

---

## Network Policies

Kyverno is installed with an **audit-only** default-deny enforcement (does not block, only reports). Two pre-configured NetworkPolicies in `k8s/040-networkpolicy.yaml`:

- `default-deny-all`: blocks all ingress/egress unless explicitly allowed
- `allow-dns`: allows egress to kube-system DNS (UDP/TCP port 53)

Add allow-rules as needed for your workloads.

---

## Deployment

K8s manifests in `k8s/` are applied to the cluster by the `deploy-k8s.yml` workflow on every push to `main` (path-filtered to `k8s/**.yaml`). The workflow logs into Azure via OIDC, fetches admin kubeconfig with `az aks get-credentials`, and runs `kubectl apply -f k8s/`. No in-cluster GitOps controller — the GitHub Actions runner is the deploy actor.

Secrets are handled separately by the `seal-secrets.yml` workflow: it reads GitHub Actions secrets, pipes them through `kubeseal` against the in-cluster Sealed Secrets controller, and `kubectl apply`s the resulting SealedSecret directly. Nothing sealed is written back to git.

---

## Current Status

- [x] Issue created and assigned to bot
- [x] /VERSIONS file — single source of truth for openclaw upstream + tool pins
- [x] Stage 1: Terraform for AKS, PV, Kyverno, Sealed Secrets, GitHub Actions
- [x] Stage 2: Custom Docker image — MCP servers (k8s, azure, aws, gcp, alicloud, debug)
- [x] Stage 2: K8s manifests (namespace, openclaw configmap, deployment, service, ingress, network policies)
- [x] Build/deploy workflows read from /VERSIONS (no hardcoded versions)
- [x] TELEGRAM_BOT_TOKEN added to secrets + configmap
- [x] README restored and updated
- [ ] **Terraform apply**: waiting for kstrassheim to merge PR #5
- [ ] **Build and push custom image**: requires mainpi.local registry credentials + docker build
- [x] **Sealed Secrets**: implemented via kubeseal CI workflow

---

## Contact

For questions, tag @kstrassheim in the issue comments.

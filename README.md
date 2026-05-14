# claw-code

Autonomous coding agent powered by OpenClaw, deployed on AKS.

## Architecture

```
claw-code/
├── main.tf                  # Terraform: AKS cluster, PV storage, ArgoCD, Kyverno
├── variables.tf             # Terraform variables
├── terraform.tfvars          # Dev environment values
├── builder/
│   ├── Dockerfile            # Custom openclaw image (all coding features, no astrology)
│   ├── k8s-mcp/             # kubectl MCP
│   ├── azure-mcp/           # az CLI MCP
│   ├── argocd-mcp/          # argocd CLI MCP
│   ├── aws-mcp/             # AWS CLI MCP
│   ├── gcp-mcp/             # gcloud MCP
│   ├── alicloud-mcp/        # aliyun CLI MCP
│   ├── debug-mcp/           # Node CDP debugger MCP
│   └── entra-totp/          # TOTP generator for Entra MFA
├── k8s/
│   ├── 000-namespace-and-config.yaml  # Namespace, ConfigMap (openclaw tools-md)
│   ├── 005-pvc.yaml          # PVC for openclaw workspace
│   ├── 010-secrets.yaml     # Secrets (placeholder values; seal for production)
│   ├── 015-openclaw-config.yaml  # ConfigMap: openclaw.json with Mistral Large + optional MiniMax
│   ├── 020-deployment.yaml  # Deployment
│   ├── 030-service.yaml     # ClusterIP service
│   ├── 040-ingress.yaml     # Ingress
│   ├── 050-networkpolicy.yaml  # default-deny + allow-dns
│   └── 060-argocd-app.yaml  # ArgoCD Application manifest
└── .github/workflows/
    ├── terraform.yml         # PR check + merge-to-main apply pipeline
    └── build-image.yml       # Build and push custom openclaw image
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
| `TF_VAR_entra_tenant_id` | Yes | Entra tenant ID (must match `AZURE_TENANT_ID`) |

> **Note on `openclaw-secrets`**: For production use, seal secrets with Sealed Secrets.
> The `k8s/010-secrets.yaml` contains placeholder values — replace before production.

---

## Stage 1: Terraform (Infrastructure)

### What gets deployed

| Resource | Purpose |
|---|---|
| AKS cluster (1x Standard_D2s_v3, 8GB RAM, 2vCPU) | K8s cluster |
| Azure Storage Account | PV (Azure Files) for openclaw workspace |
| Azure File Share (`openclaw`, 50GB) | K8s PersistentVolume |
| ArgoCD (v7.7.7) | GitOps CD, Entra-only auth |
| Kyverno (v3.3.2) | Policy engine, default-deny-all |
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
✅ ArgoCD CLI + argocd-mcp
✅ Terraform CLI + terraform-mcp-server
✅ GitHub CLI + github-mcp-server
✅ Chromium (browser automation for Entra login)
✅ podman (rootless, vfs storage)
✅ code-server (full VSCode-in-the-browser)
✅ debug-mcp (Node CDP debugger)

❌ JHora / PyJHora — NO astrology
❌ Gmail MCP
❌ Google Drive
❌ Knowledge corpus
❌ Ollama (no local LLM inference)
❌ Whisper STT
❌ Kokoro TTS
❌ Second instance (olga)

### Building the image

```bash
# Build locally (on a Linux arm64 machine with podman)
cd builder/
podman build -t mainpi.local:30500/openclaw/claw-code:<tag> \
  --build-arg BASE_IMAGE=mainpi.local:30500/openclaw/openclaw:local .
podman push mainpi.local:30500/openclaw/claw-code:<tag>

# Then update k8s/020-deployment.yaml image tag, commit, and push
# ArgoCD will auto-sync the change.
```

Or trigger the `build-image.yml` workflow via GitHub Actions (workflow_dispatch).

### Updating the image tag

After building a new image, update the `image:` section in `k8s/020-deployment.yaml`:
```yaml
image: mainpi.local:30500/openclaw/claw-code:<NEW_TAG>
```
Commit and push — ArgoCD detects the change and syncs automatically.

---

## LLMs

- **Default**: Mistral Large (`mistral-large-latest`)
- **Optional**: MiniMax (`MiniMax/MiniMax-Text-01`) — activated only when `MINIMAX_API_KEY` is present in `k8s/010-secrets.yaml`. When absent, MiniMax does not appear in the model list.

To activate MiniMax:
1. Add `MINIMAX_API_KEY: "<your-key>"` in `k8s/010-secrets.yaml`
2. Commit and push — ArgoCD syncs, pod restarts, MiniMax appears.

---

## Network Policies

Kyverno is installed with an **audit-only** default-deny enforcement (does not block, only reports). Two pre-configured NetworkPolicies in `k8s/050-networkpolicy.yaml`:

- `default-deny-all`: blocks all ingress/egress unless explicitly allowed
- `allow-dns`: allows egress to kube-system DNS (UDP/TCP port 53)

Add allow-rules as needed for your workloads.

---

## ArgoCD Access

After `terraform apply`, ArgoCD is available at `https://argocd.claw-code.internal`.

1. **OIDC login**: Click "Login with Entra ID" (or "Sign in with Microsoft" depending on ArgoCD version). The App Registration and client secret are created automatically by Terraform — no manual setup required.
2. **First login**: After the OIDC flow completes, add yourself as an admin by visiting `https://argocd.claw-code.internal/settings/users` and creating a user with `role:admin` and `email: your@email.com` matching your Entra identity.
3. The `claw-code-openclaw` Application is pre-configured in `k8s/060-argocd-app.yaml` and syncs the `k8s/` directory to the cluster on every push to `main`.

---

## Current Status

- [x] Issue created and assigned to bot
- [x] VERSIONS.md file (mirrors k8s-openclaw pattern)
- [x] Stage 1: Terraform for AKS, PV, ArgoCD, Kyverno, GitHub Actions (`feature/aks-terraform` branch)
- [x] Stage 2: Custom Docker image — all MCP servers present (k8s, azure, argocd, aws, gcp, alicloud, debug)
- [x] Stage 2: K8s manifests (namespace, PVC, secrets placeholder, openclaw configmap, deployment, service, ingress, network policies, ArgoCD app)
- [x] Build image workflow reads from VERSIONS.md (no hardcoded versions)
- [x] TELEGRAM_BOT_TOKEN added to secrets + configmap
- [x] README restored and updated
- [ ] **Terraform apply**: waiting for kstrassheim to merge PR #5
- [ ] **Build and push custom image**: requires mainpi.local registry credentials + docker build
- [ ] **Replace placeholder secrets**: `k8s/010-secrets.yaml` needs real values before production use
- [ ] **Sealed Secrets**: implement kubeseal workflow as per kstrassheim's comment

---

## Contact

For questions, tag @kstrassheim in the issue comments.

# claw-code

Autonomous coding agent powered by OpenClaw, deployed on AKS.

## Architecture

```
claw-code/
├── main.tf                  # Terraform: AKS cluster, PV storage, ArgoCD, Kyverno
├── variables.tf             # Terraform variables
├── terraform.tfvars          # Dev environment values
├── .github/workflows/
│   └── terraform.yml         # PR check + merge-to-main apply pipeline
├── k8s/
│   ├── namespace.yaml        # openclaw namespace
│   ├── openclaw-secrets.yaml # Sealed secrets for API keys
│   ├── registry-pull-secret.yaml
│   ├── pv.yaml               # Azure Files PV
│   ├── pvc.yaml              # PVC for openclaw workspace
│   ├── values.yaml           # Helm values for openclaw chart (Stage 2)
│   └── ingress.yaml          # Ingress for openclaw
└── Dockerfile               # Custom openclaw image (Stage 2)
```

## Stage 1: Infrastructure (Terraform)

### What gets deployed

| Resource | Purpose |
|---|---|
| AKS cluster (1x Standard_D2s_v3, 8GB RAM, 2vCPU) | K8s cluster |
| Azure Storage Account | PV (Azure Files) for openclaw workspace |
| Azure File Share (`openclaw`, 50GB) | K8s PersistentVolume |
| ArgoCD (v7.7.7) | GitOps CD, Entra-only auth |
| Kyverno (v3.3.2) | Policy engine, default-deny-all |
| Log Analytics Workspace | AKS monitoring |

### Prerequisites

#### Azure (to be done once by the human)

1. **Resource Group** `claw-code` must exist in the subscription:
   ```bash
   az group create --name claw-code --location westeurope
   ```

2. **User-Assigned Managed Identity** for GitHub Actions:
   ```bash
   az identity create --name github-claw-code --resource-group claw-code
   # Note down the clientId (used for AZURE_CLIENT_ID)
   ```

3. **Federated Credentials** on the managed identity (for GitHub OIDC):
   - Go to Azure Portal → Entra ID → App registrations → find your MI → Certificates & secrets → Federated credentials
   - Add credential:
     - **Trusted entity**: GitHub Actions deploying Azure resources
     - **Organization**: `kstrassheim`
     - **Repository**: `claw-code`
     - **Environment**: `dev` (or `All environments`)
     - **Audience**: `api://AzureADTokenExchange`

4. **Role assignments** on the identity:
   ```bash
   # Get the MI's principal ID
   MI_PRINCIPAL_ID=$(az identity show --name github-claw-code --resource-group claw-code --query principalId -o tsv)
   RG_ID=$(az group show --name claw-code --query id -o tsv)
   
   # Contributor on the resource group
   az role assignment create --role "Contributor" --assignee-object-id $MI_PRINCIPAL_ID --scope $RG_ID
   # Also need storage account contributor for PV
   az role assignment create --role "Storage Account Contributor" --assignee-object-id $MI_PRINCIPAL_ID --scope $RG_ID
   ```

5. **Terraform state storage** already exists at:
   - Account: `mytofustates`
   - Container: `claw-code`
   - Key: `dev.tfstate`

#### GitHub Secrets

Set these in `Settings → Secrets and variables → Actions`:

| Secret | Value |
|---|---|
| `AZURE_CLIENT_ID` | Client ID of `github-claw-code` managed identity |
| `AZURE_TENANT_ID` | Your Entra tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription ID |

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

## Stage 2: OpenClaw Deployment (Docker + K8s Helm)

### Custom Image Features

The `claw-code` openclaw image includes all coding features from `k8s-openclaw`, minus:
- ❌ JHora / astrology Python packages
- ❌ Gmail MCP
- ❌ Google Drive
- ❌ Knowledge corpus
- ❌ Ollama (LLM local inference)
- ❌ Whisper STT
- ❌ Kokoro TTS
- ❌ OpenWebUI
- ❌ Second instance (olga)

✅ Included:
- VSCode (code-server)
- Azure CLI + azure-mcp
- AWS CLI v2 + aws-mcp
- Google Cloud CLI + gcp-mcp
- Alibaba Cloud CLI + alicloud-mcp
- kubectl + k8s-mcp
- ArgoCD CLI + argocd-mcp
- Terraform CLI + terraform-mcp-server
- GitHub CLI + github-mcp-server
- Chromium (for browser automation / Entra login flows)
- podman (rootless, vfs storage)
- debug-mcp (Node CDP debugger)

### LLMs

- **Default**: Mistral Large
- **Optional**: MiniMax — activated only when `MINIMAX_API_KEY` is present in `openclaw-secrets`. When absent, MiniMax is not shown in the model list.

### Deployment

OpenClaw is deployed via its Helm chart with the values in `k8s/values.yaml`. ArgoCD manages it via GitOps — any change to the `k8s/` directory triggers a sync.

### Required GitHub Secrets (for Stage 2)

| Secret | Value |
|---|---|
| `OPENCLAW_IMAGE_TAG` | e.g. `v2026.5.7.14` — set when the custom image is built and pushed |
| `MINIMAX_API_KEY` | (optional) Only if you want MiniMax enabled |
| `MISTRAL_API_KEY` | Required for Mistral Large |

### Building the image

See `builder/README.md` in the `k8s-openclaw` repo for the build pipeline. The custom image for `claw-code` is built from the same `builder/Dockerfile` but with:
- Base image: the same upstream openclaw image
- No JHora/astrology layer
- Image tag pushed to `mainpi.local:30500/openclaw/claw-code`

---

## Kyverno Policies

Kyverno is installed with a **default-deny-all NetworkPolicy** on the `openclaw` namespace. Only DNS egress is pre-allowed. Add specific allow rules as needed for your workloads.

To switch Kyverno to **enforce** mode (instead of audit-only):

```bash
# Edit the Kyverno helm values in main.tf:
# backgroundScanning.enforcement = "Enforce"
# Then terraform apply
```

---

## ArgoCD Access

After `terraform apply`, ArgoCD is available at `https://argocd.claw-code.internal`.

1. **Initial login**: Username `admin`, password `admin` (change it immediately).
2. **Add your GitHub account** as an OIDC user in Entra ID (enterprise application).
3. **ArgoCD URL** for this project: configure in ArgoCD UI or via the Application manifest.

---

## Current Status

- [x] Issue created and assigned to bot
- [x] Terraform base structure written
- [x] GitHub Actions CI/CD pipeline for Terraform
- [x] Terraform apply on dev environment (waiting for RBAC/setup)
- [ ] Stage 2: Custom Docker image + K8s Helm chart
- [ ] README documentation finalization

---

## Contact

For questions about this setup, tag @kstrassheim in the issue comments.
# claw-code

Autonomous coding agent powered by OpenClaw, deployed on AKS.

## Architecture Overview

**Infrastructure (Terraform / `main.tf`):**
- AKS cluster 1x Standard_D2s_v3, kubernetes 1.31, location westeurope
- Azure Storage Account (`var.storage_account_name`, e.g. `clwcodecodev`) for PV (Azure Files)
- Azure File Share `openclaw` (50GB) mounted as PersistentVolume
- ArgoCD 7.7.7 — Entra/OIDC-only auth (no local admin password)
- Kyverno 3.3.2 — audit-only by default (reports violations, does NOT block)
- GitHub Actions OIDC via `github-claw-code` managed identity

**Runtime (k8s/ manifests):**
- Custom openclaw Docker image running on the cluster
- Sealed Secrets for all sensitive configuration
- NetworkPolicy default-deny + explicit allow for DNS + HTTPS/443

## Repository Structure

```
claw-code/
├── main.tf                  # Terraform: AKS, PV storage, ArgoCD, Kyverno
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
│   ├── 000-namespace-and-config.yaml  # Namespace, ConfigMap (openclaw.json seed)
│   ├── 010-secrets.yaml     # Sealed secrets (API keys, gateway token)
│   ├── 020-deployment.yaml  # Deployment with init containers
│   ├── 030-service.yaml     # ClusterIP service
│   ├── 040-ingress.yaml     # Ingress
│   ├── 050-networkpolicy.yaml  # default-deny + allow-dns + allow-https
│   └── 060-argocd-app.yaml  # ArgoCD Application manifest
└── .github/workflows/
    ├── terraform.yml         # PR check + merge-to-main apply pipeline
    └── build-image.yml       # Build and push custom openclaw image
```

---

## Prerequisites (one-time human setup)

### 1. Azure resources

```bash
# Resource group (if not already existing)
az group create --name claw-code --location westeurope

# User-Assigned Managed Identity for GitHub Actions OIDC
az identity create --name github-claw-code --resource-group claw-code
# Note the clientId as AZURE_CLIENT_ID

# Federated Credentials on the identity
# Azure Portal → Entra ID → App registrations → find github-claw-code MI
# → Certificates & secrets → Federated credentials → Add credential:
#   - Trusted entity: GitHub Actions
#   - Organization: kstrassheim
#   - Repository: claw-code
#   - Environment: dev (or All environments)
#   - Audience: api://AzureADTokenExchange

# Role assignments
MI_PRINCIPAL_ID=$(az identity show --name github-claw-code --resource-group claw-code --query principalId -o tsv)
RG_ID=$(az group show --name claw-code --query id -o tsv)
az role assignment create --role "Contributor" --assignee-object-id $MI_PRINCIPAL_ID --scope $RG_ID
az role assignment create --role "Storage Account Contributor" --assignee-object-id $MI_PRINCIPAL_ID --scope $RG_ID
```

### 2. Entra app registration for ArgoCD OIDC

ArgoCD uses Entra ID as its sole identity provider (no local admin password).
You must register an Entra application:

1. **Azure Portal → Entra ID → App registrations → New registration**
   - Name: `claw-code-argocd`
   - Supported account types: "Accounts in this organizational directory only"
   - Redirect URI: Web → `https://argocd.claw-code.internal/auth/callback`

2. **Expose an API** (left sidebar)
   - Set Application ID URI: `api://<your-client-id-from-overview-page>`
   - Add a scope: `ArgoCD` (display name: `Access ArgoCD`)

3. **Certificates & secrets → New client secret**
   - Copy the secret value — this is `AZ_CLIENT_SECRET` in GitHub Actions secrets

4. **Manifest** — find `acceptMappedClaims` in the JSON editor, set to `true`

5. **API permissions** (if needed):
   - Microsoft Graph → `openid`, `profile`, `email`

6. In GitHub Actions secrets, add:
   - `AZ_CLIENT_ID` = managed identity client ID (`github-claw-code`)
   - `AZ_CLIENT_SECRET` = the client secret from step 3 above
   - `TF_VAR_entra_client_secret` = same value (passed to Terraform)
   - `TF_VAR_entra_tenant_id` = Entra tenant ID (same as `AZURE_TENANT_ID`)

### 3. Terraform state storage (already exists)

- Account: `mytofustates`
- Container: `claw-code`
- Key: `dev.tfstate`
- This is separate from the PV storage account (`clwcodecodev`)

### 4. GitHub Actions secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `AZURE_CLIENT_ID` | Yes | Client ID of `github-claw-code` managed identity |
| `AZURE_TENANT_ID` | Yes | Entra tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Subscription ID |
| `AZ_CLIENT_ID` | Yes | Same as `AZURE_CLIENT_ID` (used by ArgoCD OIDC) |
| `AZ_CLIENT_SECRET` | Yes | Entra app client secret (from step 2 above) |
| `TF_VAR_entra_client_secret` | Yes | Passed to Terraform as `entra_client_secret` var |
| `TF_VAR_entra_tenant_id` | Yes | Entra tenant ID (same as `AZURE_TENANT_ID`) |
| `MISTRAL_API_KEY` | Yes | Mistral Large API key |
| `GITHUB_TOKEN` | Yes | Bot account PAT (with repo access) |
| `OPENCLAW_GATEWAY_TOKEN` | Yes | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `REGISTRY_USERNAME` | Yes | mainpi.local registry username |
| `REGISTRY_PASSWORD` | Yes | mainpi.local registry password |
| `MINIMAX_API_KEY` | No | If present, MiniMax appears in model list |

### 5. Optional: Entra group for cluster admin RBAC

If you want your Entra group to have cluster-admin access on the AKS cluster,
add the group's Object ID to `admin_group_object_ids` in `main.tf`:

```hcl
azure_active_directory_role_based_access_control {
  managed               = true
  azure_rbac_enabled     = true
  admin_group_object_ids = ["<YOUR-ENTRA-GROUP-OBJECT-ID>"]
}
```

To find your group's Object ID:
```bash
az ad group show --group "<your-group-name>" --query objectId -o tsv
```

---

## Stage 1: Terraform (Infrastructure)

### What gets deployed

| Resource | Purpose |
|---|---|
| AKS cluster (1x Standard_D2s_v3, 8GB RAM, 2vCPU, k8s 1.31) | K8s cluster |
| Azure Storage Account (`var.storage_account_name`) | PV (Azure Files) for openclaw workspace |
| Azure File Share (`openclaw`, 50GB) | K8s PersistentVolume |
| ArgoCD (v7.7.7) | GitOps CD, Entra/OIDC-only auth |
| Kyverno (v3.3.2) | Policy engine, audit-only default-deny |
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
| `storage_account_name` | `clwcodecodev` | PV storage account name (globally unique, used for Azure Files) |
| `namespace` | `openclaw` | K8s namespace |
| `entra_client_secret` | (required) | Entra app client secret for ArgoCD OIDC |
| `entra_tenant_id` | (required) | Entra tenant ID |

**Important**: The PV storage account (`clwcodecodev`) is DIFFERENT from the Terraform state backend storage account (`mytofustates`). They serve different purposes.

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

---

## LLMs

- **Default**: Mistral Large (`mistral-large-latest`)
- **Optional**: MiniMax (`MiniMax/MiniMax-Text-01`) — activated only when `MINIMAX_API_KEY` is present in secrets. When absent, MiniMax does not appear in the model list.

---

## Network Policies

Kyverno runs in **AUDIT mode** — it reports violations but does NOT block pods.
To switch to ENFORCE mode (which blocks non-compliant pods):

```yaml
# In main.tf, change the Kyverno helm_release values:
backgroundScanning:
  enabled: true
  enforcement: "Enforce"   # <-- change from "Audit" to "Enforce"
```

Then add PolicyExceptions for allowed workloads before applying.

Three pre-configured NetworkPolicies in `k8s/`:
- `default-deny-all`: blocks all ingress/egress unless explicitly allowed
- `allow-dns`: allows egress to kube-system DNS (UDP/TCP port 53)
- `allow-https`: allows egress to TCP 443 (cloud APIs, OIDC, image pulls)

---

## ArgoCD Access

ArgoCD uses **Entra/OIDC-only login** — no local admin password is generated.

1. **First login** at `https://argocd.claw-code.internal`:
   - Click "Sign in with Entra ID" (or similar OIDC button)
   - Authenticate with your Entra account
   - If access is denied: your Entra user/group must be added to the
     `argocd-admins` role via the `configs.rbac.policy.csv` in `main.tf`
2. **Add members to ArgoCD**:
   - Edit `configs.rbac.policy.csv` in `main.tf` to add Entra group Object IDs
   - Example: `g, <YOUR-GROUP-OBJECT-ID>, role:admin`
   - Terraform apply → ArgoCD syncs automatically

---

## Secrets Management (Sealed Secrets)

All sensitive values in `k8s/010-secrets.yaml` are pre-encrypted as
SealedSecrets (Bitnami). The SealedSecrets controller is installed via
ArgoCD and the public key lives in the `openclaw` namespace.

To re-encrypt secrets after changing values:
```bash
# Get the cluster's SealedSecrets public key
kubectl get secret -n openclaw -o yaml sealed-secrets-key-<hash>

# Use kubeseal to encrypt new values
kubeseal --cert=<path-to-cert> --format=yaml < secrets.yaml > sealed-secrets.yaml
```

---

## Current Status

- [x] Issue created and assigned to bot
- [x] Stage 1: Terraform for AKS, PV, ArgoCD, Kyverno, GitHub Actions
- [x] Stage 1 fix: ArgoCD Entra/OIDC-only auth (no local admin password)
- [x] Stage 1 fix: PV storage account naming (clwcodecodev, not mytofustates)
- [x] Stage 1 fix: Kyverno audit-mode documentation added
- [x] Stage 2: Custom Docker image (builder/Dockerfile + all MCP packages)
- [x] Stage 2: K8s manifests (namespace, secrets, deployment, service, ingress, network policies, ArgoCD app)
- [x] Build image workflow (`.github/workflows/build-image.yml`)
- [x] README updated with Entra app registration instructions
- [ ] **Terraform apply**: run by human or GitHub Actions after merging to `main`
- [ ] **Build and push custom image**: requires mainpi.local registry credentials
- [ ] **Replace placeholder secrets**: `k8s/010-secrets.yaml` needs real values before production use
- [ ] **Register Entra app** for ArgoCD OIDC (see Prerequisites step 2)

---

## Contact

For questions, tag @kstrassheim in the issue comments.
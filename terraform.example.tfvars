# =============================================================================
# Stage 1: Infrastructure — Terraform variables (dev environment)
# =============================================================================
# Copy to terraform.tfvars and fill in real values.
# DO NOT commit real secrets to git — use GitHub Actions secrets + TF_VAR_* instead.

env                  = "dev"
cluster_name         = "claw-code-aks"
location             = "westeurope"
node_size            = "Standard_D2s_v3"
node_count           = 1
storage_account_name = "clwcodecodev"  # PV storage (Azure Files), globally unique, NOT mytofustates
namespace            = "openclaw"

# ArgoCD OIDC — register an Entra app first (see README.md Prerequisites step 2)
# Pass real values via GitHub Actions secrets as TF_VAR_entra_client_secret / TF_VAR_entra_tenant_id
# TF_VAR_entra_tenant_id is already set in the workflow from AZURE_TENANT_ID secret
entra_client_secret   = ""  # Set via TF_VAR_entra_client_secret in GitHub Actions secrets
entra_tenant_id       = ""  # Set via TF_VAR_entra_tenant_id in GitHub Actions secrets
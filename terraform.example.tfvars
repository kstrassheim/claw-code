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

# ArgoCD OIDC — the App Registration is created automatically by Terraform.
# Only TF_VAR_entra_tenant_id is needed (already set from AZURE_TENANT_ID in the workflow).
entra_tenant_id       = ""  # Set via TF_VAR_entra_tenant_id in GitHub Actions secrets
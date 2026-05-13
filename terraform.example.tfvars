# =============================================================================
# Stage 1: Infrastructure — Terraform variables
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
entra_client_secret   = "REPLACE_WITH_ENTRA_CLIENT_SECRET"  # e.g. from Entra app → Certificates & secrets
entra_tenant_id       = "REPLACE_WITH_ENTRA_TENANT_ID"       # e.g. 1e1e851f-618f-40d4-9c2d-45355ad039a9
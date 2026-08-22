variable "cosmos_location" {
  description = <<-EOT
    Region for the planning-store Cosmos account.

    Its own variable because it has needed to differ: West Europe refused new
    accounts twice from a clean start, with zone redundancy already off, and
    Azure names zone redundancy in that error regardless of what was asked for.
    It is a regional capacity limit that no setting here can satisfy.

    Switzerland North was verified by creating a throwaway account there before
    committing to it, so this now matches var.location and everything lives in
    one region. The variable stays because the next capacity limit will not
    announce itself either.
  EOT
  type        = string
  default     = "switzerlandnorth"
}


variable "cosmos_account_name" {
  description = <<-EOT
    Cosmos account name for the planning store.

    Must be globally unique across Azure, 3-44 characters, lowercase letters,
    digits and hyphens only — the account name is part of the endpoint
    hostname, so a clash with somebody else's account fails at apply time
    rather than at plan time.
  EOT
  type        = string
  default     = "clawcode-planning-dev"
}

variable "app_name" {
  description = <<-EOT
    Canonical project name, and the single source of truth for two things that
    must agree:

      * the blob container in the mytofustates storage account holding this
        project's state (backend.container_name),
      * the RSA key in the kv-mytofustates Key Vault that wraps the state
        encryption data key (encryption.key_provider.vault_key_name).

    Referencing a variable from the backend block is OpenTofu-only (early
    evaluation, resolved at `tofu init` before state exists) — HashiCorp
    Terraform cannot parse it.
  EOT
  type        = string
  default     = "claw-code"
}

variable "use_oidc" {
  description = <<-EOT
    Authenticate the azure_vault state-encryption key provider with a GitHub
    OIDC token instead of the Azure CLI. Defaults to false so local runs use
    your `az login` session; CI sets TF_VAR_use_oidc=true.
  EOT
  type        = bool
  default     = false
}

variable "arm_client_id" {
  description = "Client ID for the key provider when use_oidc is true. Empty locally; CI sets TF_VAR_arm_client_id."
  type        = string
  default     = ""
}

variable "arm_tenant_id" {
  description = "Tenant ID for the key provider when use_oidc is true. Empty locally; CI sets TF_VAR_arm_tenant_id."
  type        = string
  default     = ""
}

variable "env" {
  description = "Environment name"
  default     = "dev"
  type        = string
}

variable "cluster_name" {
  description = "AKS cluster name"
  default     = "claw-code-aks"
  type        = string
}

variable "location" {
  description = "Azure region"
  default     = "switzerlandnorth"
  type        = string
}

variable "node_size" {
  description = <<-EOT
    VM size for the AKS node pool. arm64, matching the image build target
    (linux/arm64) and cheaper than the x86 equivalent.

    Cobalt v6 (Standard_D2ps_v6) rather than the v5 it replaced: newer silicon
    at the same 2 vCPU / 8 GiB. The v6 `ps` line has no local temp disk, which
    costs nothing here because the node pool already uses a managed OS disk.
  EOT
  default     = "Standard_D2ps_v6"
  type        = string
}

variable "node_count" {
  description = "Number of nodes in the default pool"
  default     = 1
  type        = number
}

variable "storage_account_name" {
  description = "Storage account name for Azure Files PV (must be globally unique, ~24 chars max, e.g. clwcodecodev)"
  default     = "clwcodecodev"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace for claw-code"
  default     = "claw-code"
  type        = string
}

variable "aks_admin_group_name" {
  description = "Display name of the Entra ID security group granted AKS cluster-admin RBAC. The group must already exist; Terraform only references it by name."
  type        = string
  default     = "claw-code-aks-admin"
}

variable "unique_suffix" {
  description = "Unique suffix appended to globally-unique resource names (max 4 chars, e.g. 'dev1'). Used to make storage account and ACR names unique across deployments."
  type        = string
  default     = "dev2"
}
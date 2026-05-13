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
  default     = "westeurope"
  type        = string
}

variable "node_size" {
  description = "VM size for AKS node pool"
  default     = "Standard_D2s_v3"
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
  description = "Kubernetes namespace for openclaw"
  default     = "openclaw"
  type        = string
}

variable "entra_client_secret" {
  description = "Entra app client secret for ArgoCD OIDC authentication"
  type        = string
  sensitive   = true
  default     = ""
}

variable "entra_tenant_id" {
  description = "Entra tenant ID for ArgoCD OIDC"
  type        = string
  default     = ""
}
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
  description = "VM size for AKS node pool. Default is arm64 (Standard_D2pds_v5, 2 vCPU / 8 GiB) — matches the openclaw image build target (linux/arm64) and is ~$20/mo cheaper than the x86 D2s_v3 equivalent."
  default     = "Standard_D2pds_v5"
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

variable "aks_admin_group_name" {
  description = "Display name of the Entra ID security group granted AKS cluster-admin RBAC. The group must already exist; Terraform only references it by name."
  type        = string
  default     = "claw-code-aks-admin"
}

variable "unique_suffix" {
  description = "Unique suffix appended to globally-unique resource names (max 4 chars, e.g. 'dev1'). Used to make storage account and ACR names unique across deployments."
  type        = string
  default     = "dev1"
}
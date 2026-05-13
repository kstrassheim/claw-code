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
  description = "Storage account name for Terraform state and PV"
  default     = "clwcodestate"  # must be globally unique, ~24 chars max
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace for openclaw"
  default     = "openclaw"
  type        = string
}
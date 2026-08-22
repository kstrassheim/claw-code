provider "azurerm" {
  features {}
}

provider "azuread" {}

# Kubernetes/Helm credentials are read straight off the AKS resource rather
# than from a kubeconfig file on disk.
#
# `config_path = "/tmp/kubeconfig_clawcode"` could not work on the run that
# CREATES the cluster: the file is written by an `az aks get-credentials` step
# in the deploy, so on a first apply it does not exist and both providers fall
# back to their default host — every kubernetes_* resource then fails with
# "dial tcp [::1]:80: connect: connection refused" against localhost:80.
#
# Sourcing from the cluster resource makes the credentials an attribute, so
# they resolve after the cluster is created and the whole thing converges in a
# single apply. kube_admin_config is the cert-based local admin credential (no
# kubelogin needed); it is populated because local_account_disabled is not set.
# Entra RBAC still governs normal user access via azure_rbac_enabled.
provider "kubernetes" {
  host                   = azurerm_kubernetes_cluster.aks.kube_admin_config[0].host
  client_certificate     = base64decode(azurerm_kubernetes_cluster.aks.kube_admin_config[0].client_certificate)
  client_key             = base64decode(azurerm_kubernetes_cluster.aks.kube_admin_config[0].client_key)
  cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.aks.kube_admin_config[0].cluster_ca_certificate)
}

provider "helm" {
  kubernetes = {
    host                   = azurerm_kubernetes_cluster.aks.kube_admin_config[0].host
    client_certificate     = base64decode(azurerm_kubernetes_cluster.aks.kube_admin_config[0].client_certificate)
    client_key             = base64decode(azurerm_kubernetes_cluster.aks.kube_admin_config[0].client_key)
    cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.aks.kube_admin_config[0].cluster_ca_certificate)
  }
}

locals {
  resource_group_name  = "claw-code"
  cluster_name         = var.cluster_name
  location             = var.location
  storage_account_name = var.storage_account_name
  namespace            = var.namespace
  aks_version          = "1.35.3"
}

# Lookup the AKS admin group by display name. The object ID changes per
# tenant, but the group name is stable — easier to keep this in tfvars.
data "azuread_group" "aks_admin" {
  display_name     = var.aks_admin_group_name
  security_enabled = true
}

# Get existing resource group
data "azurerm_resource_group" "rg" {
  name = local.resource_group_name
}

# Create the claw-code namespace (needed for NetworkPolicies and later K8s resources)
resource "kubernetes_namespace" "claw-code" {
  metadata {
    name = var.namespace
    labels = {
      "environment" = var.env
      "project"     = "claw-code"
    }
  }
  # Namespace was pre-created manually — don't fail if it already exists
  lifecycle {
    ignore_changes = [metadata]
  }
}

# Get the deploy managed identity (deploy-claw-code)
data "azurerm_user_assigned_identity" "deploy_identity" {
  name                = "deploy-claw-code"
  resource_group_name = data.azurerm_resource_group.rg.name
}

# =============================================================================
# PV Storage Account
# Note: clwcodecodev was pre-created with AzureADDomainService identity-based
# auth. Terraform manages only the share (azurerm_storage_share) below.
# =============================================================================
resource "azurerm_storage_account" "pv" {
  name                       = local.storage_account_name
  resource_group_name        = data.azurerm_resource_group.rg.name
  location                   = data.azurerm_resource_group.rg.location
  account_tier               = "Standard"
  account_replication_type   = "LRS"
  min_tls_version            = "TLS1_2"
  https_traffic_only_enabled = true

  tags = {
    environment = var.env
    project     = "claw-code"
  }
}

# Create a file share in the storage account (for K8s PV)
resource "azurerm_storage_share" "claw_code" {
  name               = "claw-code"
  storage_account_id = azurerm_storage_account.pv.id
  quota              = 50 # 50 GiB
}

# =============================================================================
# Azure Container Registry — for the custom claw-code image
# =============================================================================
resource "azurerm_container_registry" "acr" {
  name                = "clwcodecodev${var.unique_suffix}" # clwcodecodev + suffix, max 50 chars
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = data.azurerm_resource_group.rg.location
  sku                 = "Basic"
  admin_enabled       = false # Entra-only auth, no admin user

  public_network_access_enabled = true # AKS must be able to pull; restrict via NSG rules if needed

  tags = {
    environment = var.env
    project     = "claw-code"
  }
}

# Grant the deploy identity AcrPull (used by GitHub Actions for `az acr login`
# and by `az acr import` when mirroring the upstream claw-code-base).
resource "azurerm_role_assignment" "acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = data.azurerm_user_assigned_identity.deploy_identity.principal_id
}

# Grant the AKS *kubelet* identity AcrPull. AKS uses a separate auto-created
# kubelet managed identity (NOT the cluster's SystemAssigned identity, NOT
# the deploy identity above) to pull images for pods. Without this role the
# kubelet hits "401 Unauthorized" when trying to pull from our ACR.
resource "azurerm_role_assignment" "acr_pull_aks_kubelet" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
}

output "container_registry_login_server" {
  description = "ACR login server for docker push/pull"
  value       = azurerm_container_registry.acr.login_server
}

# =============================================================================
# AKS Cluster
# =============================================================================
resource "azurerm_kubernetes_cluster" "aks" {
  name                = local.cluster_name
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = data.azurerm_resource_group.rg.location
  dns_prefix          = "claw-code"
  kubernetes_version  = local.aks_version
  sku_tier            = "Free"

  # Workload identity. Both flags are required for a pod to exchange its
  # ServiceAccount token for an Azure AD token: the OIDC issuer publishes the
  # keys Azure validates the token against, and the mutating webhook projects
  # the token into the pod. Without them the federated credential in
  # cosmosdb.tf has no issuer to trust and every Cosmos call is unauthorised.
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  default_node_pool {
    name            = "default"
    vm_size         = var.node_size
    node_count      = var.node_count
    os_disk_size_gb = 30
    os_disk_type    = "Managed"
    type            = "VirtualMachineScaleSets"
    # Required by the azurerm provider when changing vm_size (or any
    # other field that forces a pool rotation): AKS creates a temp pool
    # under this name, migrates workloads, deletes the old pool, then
    # renames the temp pool back to "default". Pure machinery — never
    # appears as the running pool name.
    temporary_name_for_rotation = "tmpdefault"
  }

  identity {
    type = "SystemAssigned"
  }

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled     = true
    admin_group_object_ids = [data.azuread_group.aks_admin.object_id]
  }

  key_vault_secrets_provider {
    secret_rotation_enabled = false
  }

  network_profile {
    network_plugin    = "azure"
    load_balancer_sku = "standard"
    outbound_type     = "loadBalancer"
  }

  tags = {
    environment = var.env
    project     = "claw-code"
  }
}

# Grant claw-code-aks-admin group Azure Kubernetes Service RBAC Cluster Admin on the cluster scope.
# This allows Entra-authenticated users (via kubelogin) to access the cluster.
# Note: azure_rbac_enabled = true is set on the AKS cluster, so Azure RBAC governs access.
resource "azurerm_role_assignment" "aks_admin" {
  scope                = azurerm_kubernetes_cluster.aks.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = data.azuread_group.aks_admin.object_id
}

# NOTE: Role assignments for the deploy identity (Storage Blob Data Contributor on
# mytofustates and Storage Account Contributor on the PV storage account) must be
# granted manually by an Owner, or via a separate privileged identity.
# Skipped here — requires Owner-level permissions.

# =============================================================================
# Default-deny-all NetworkPolicy — blocks ALL ingress/egress by default.
# Add explicit allow policies for each required access pattern.
# =============================================================================
resource "kubernetes_network_policy_v1" "default_deny_all" {
  metadata {
    name      = "default-deny-all"
    namespace = local.namespace
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

# Allow DNS egress (required for cluster DNS resolution)
resource "kubernetes_network_policy_v1" "allow_dns" {
  metadata {
    name      = "allow-dns"
    namespace = local.namespace
  }
  spec {
    pod_selector {}
    egress {
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "kube-system"
          }
        }
      }
      ports {
        protocol = "UDP"
        port     = "53"
      }
      ports {
        protocol = "TCP"
        port     = "53"
      }
    }
    policy_types = ["Egress"]
  }
}

# Allow HTTPS/443 egress (needed for cloud API calls, OIDC, image pulls, etc.)
resource "kubernetes_network_policy_v1" "allow_https" {
  metadata {
    name      = "allow-https"
    namespace = local.namespace
  }
  spec {
    pod_selector {}
    egress {
      ports {
        protocol = "TCP"
        port     = "443"
      }
    }
    policy_types = ["Egress"]
  }
}

# =============================================================================
# Terraform Output
# =============================================================================
output "aks_cluster_name" {
  description = "AKS cluster name"
  value       = azurerm_kubernetes_cluster.aks.name
}

output "aks_fqdn" {
  description = "AKS cluster FQDN"
  value       = azurerm_kubernetes_cluster.aks.fqdn
}

output "storage_account_name" {
  description = "PV storage account name (Entra-only auth, no keys)"
  value       = azurerm_storage_account.pv.name
}

output "storage_share_name" {
  description = "PV storage share name"
  value       = azurerm_storage_share.claw_code.name
}

output "kubeconfig" {
  description = "Raw kubeconfig for the AKS cluster"
  value       = azurerm_kubernetes_cluster.aks.kube_config_raw
  sensitive   = true
}

output "deploy_identity_client_id" {
  description = "Client ID of the deploy-claw-code MI (used for Entra app registration OIDC clientID)"
  value       = data.azurerm_user_assigned_identity.deploy_identity.client_id
}

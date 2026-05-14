terraform {
  required_version = ">= 1.3"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
  }

  backend "azurerm" {
    resource_group_name  = "terraform"
    storage_account_name = "mytofustates"
    container_name       = "claw-code"
    key                  = "dev.tfstate"
    use_azuread_auth = true
  }
}

provider "azurerm" {
  features {}
}

provider "azuread" {}

provider "kubernetes" {
  # For CI: set env vars ARM_USE_OIDC=true, ARM_USE_AZUREAD_AUTH=true, then run
  # `kubelogin convert-kubeconfig -l azurecli` to use Entra auth (requires kubelogin binary).
  # For local dev without AAD: use the admin kubeconfig directly.
  # We use the admin kubeconfig here so Terraform can run locally without kubelogin.
  config_path = "/tmp/kubeconfig_clawcode"
}

provider "helm" {
  kubernetes {
    config_path = "/tmp/kubeconfig_clawcode"
  }
}

locals {
  resource_group_name    = "claw-code"
  cluster_name           = var.cluster_name
  location               = var.location
  storage_account_name   = var.storage_account_name
  namespace              = var.namespace
  aks_version            = "1.35.3"
  admin_group_object_ids = ["11755bb8-1adf-4c08-9424-0aecf3b6952e"]
  entra_tenant_id        = var.entra_tenant_id
}

# Get existing resource group
data "azurerm_resource_group" "rg" {
  name = local.resource_group_name
}

# Create the openclaw namespace (needed for NetworkPolicies and later K8s resources)
resource "kubernetes_namespace" "openclaw" {
  metadata {
    name = local.namespace
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
  name                                             = local.storage_account_name
  resource_group_name                              = data.azurerm_resource_group.rg.name
  location                                         = data.azurerm_resource_group.rg.location
  account_tier                                     = "Standard"
  account_replication_type                         = "LRS"
  min_tls_version                                  = "TLS1_2"
  https_traffic_only_enabled = true

  tags = {
    environment = var.env
    project     = "claw-code"
  }
}

# Create a file share in the storage account (for K8s PV)
resource "azurerm_storage_share" "openclaw" {
  name             = "openclaw"
  storage_account_id = azurerm_storage_account.pv.id
  quota             = 50  # 50 GiB
}

# =============================================================================
# Azure Container Registry — for the custom openclaw image
# =============================================================================
resource "azurerm_container_registry" "acr" {
  name                   = "clwcodecodev${var.unique_suffix}"  # clwcodecodev + suffix, max 50 chars
  resource_group_name    = data.azurerm_resource_group.rg.name
  location               = data.azurerm_resource_group.rg.location
  sku                    = "Basic"
  admin_enabled          = false  # Entra-only auth, no admin user

  public_network_access_enabled = true  # AKS must be able to pull; restrict via NSG rules if needed

  tags = {
    environment = var.env
    project     = "claw-code"
  }
}

# Grant deploy identity AcrPull on the registry so AKS can pull images
resource "azurerm_role_assignment" "acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = data.azurerm_user_assigned_identity.deploy_identity.principal_id
}

output "container_registry_login_server" {
  description = "ACR login server for docker push/pull"
  value       = azurerm_container_registry.acr.login_server
}

# =============================================================================
# Entra App Registration for ArgoCD OIDC
# Created automatically by Terraform — no manual registration needed.
# The client secret is stored in the azuread_application_password resource
# and passed to ArgoCD via the argocd Helm values.
# =============================================================================
resource "azuread_application" "argocd" {
  display_name     = "claw-code-argocd"
  sign_in_audience  = "AzureADMyOrg"

  owners = [data.azurerm_user_assigned_identity.deploy_identity.principal_id]

  api {
    mapped_claims_enabled          = true
    requested_access_token_version = 2

    oauth2_permission_scope {
      admin_consent_description  = "ArgoCD server access"
      admin_consent_display_name = "ArgoCD Access"
      enabled                    = true
      id                         = "4f152ac3-0d01-4f1d-9e9b-c4b8a5d6f0a8"
      type                       = "User"
      value                      = "access"
    }
  }
}

# Generate a client secret for the ArgoCD App Registration
resource "azuread_application_password" "argocd" {
  application_id = azuread_application.argocd.id
  display_name   = "ArgoCD OIDC Client Secret"
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

  default_node_pool {
    name              = "default"
    vm_size           = var.node_size
    node_count        = var.node_count
    os_disk_size_gb   = 30
    os_disk_type      = "Managed"
    type              = "VirtualMachineScaleSets"
  }

  identity {
    type = "SystemAssigned"
  }

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled     = true
    admin_group_object_ids = local.admin_group_object_ids
  }

  key_vault_secrets_provider {
    secret_rotation_enabled = false
  }

  network_profile {
    network_plugin     = "azure"
    load_balancer_sku  = "standard"
    outbound_type      = "loadBalancer"
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
  principal_id         = local.admin_group_object_ids[0]  # "11755bb8-1adf-4c08-9424-0aecf3b6952e"
}

# NOTE: Role assignments for the deploy identity (Storage Blob Data Contributor on
# mytofustates and Storage Account Contributor on the PV storage account) must be
# granted manually by an Owner, or via a separate privileged identity.
# Skipped here — requires Owner-level permissions.

# =============================================================================
# ArgoCD Installation via Helm — Entra/OIDC-only auth (no local passwords)
# =============================================================================
# The ArgoCD App Registration (client ID + secret) is created automatically by Terraform.
# No manual registration needed. The secret is generated via azuread_application_password
# and passed directly into the ArgoCD Helm values.
resource "helm_release" "argocd" {
  name       = "argocd"
  repository = "https://argoproj.github.io/argo-helm/"
  chart      = "argo-cd"
  version    = "7.7.7"
  namespace  = "argocd"
  create_namespace = true

  values = [<<EOF
server:
  ingress:
    enabled: true
    className: nginx
    annotations:
      nginx.ingress.kubernetes.io/ssl-redirect: "false"
      nginx.ingress.kubernetes.io/backend-protocol: "HTTPS"
    hosts:
      - argocd.claw-code.internal
    path: /
    tls:
      - secretName: argocd-tls
        hosts:
          - argocd.claw-code.internal

configs:
  rbac:
    policy.default: "role:readonly"
    policy.csv: |
      g, system:authenticated, role:readonly
      g, argocd-admins, role:admin

dex:
  enabled: false

notifications:
  enabled: false

server:
  replicas: 1
  extraArgs:
    - --insecure
  configMap:
    "oidc.config": |
      name: Entra ID
      issuer: https://login.microsoftonline.com/${local.entra_tenant_id}/oauth2/v2.0
      clientID: ${azuread_application.argocd.client_id}
      clientSecret: ${azuread_application_password.argocd.value}
      requestedScopes:
        - openid
        - profile
        - email
        - offline_access
        - api://${azuread_application.argocd.client_id}/access
      requestedAudiences:
        - api://${azuread_application.argocd.client_id}/access

url: https://argocd.claw-code.internal
controller:
  replicas: 1
repoServer:
  replicas: 1
EOF
  ]

  depends_on = [azurerm_kubernetes_cluster.aks]
}

# =============================================================================
# Kyverno Installation via Helm with default-deny-all policy
# =============================================================================
# NOTE: Kyverno is running in AUDIT mode (backgroundScanning.enforcement: Audit).
# It will NOT block pods — only report violations. To enforce policies,
# change backgroundScanning.enforcement to "Enforce" and add
# PolicyExceptions for allowed workloads.

# =============================================================================
# Sealed Secrets Controller — installed via Helm so the kubeseal CLI
# (used by the CI sealing job) can fetch the certificate from the cluster.
# =============================================================================
resource "helm_release" "sealed_secrets" {
  name       = "sealed-secrets"
  repository = "https://bitnami-labs.github.io/sealed-secrets"
  chart      = "sealed-secrets"
  version    = "2.16.2"
  namespace  = "kube-system"
  create_namespace = false

  set {
    name  = "secret-name"
    value = ""
  }
}
resource "helm_release" "kyverno" {
  name       = "kyverno"
  repository = "https://kyverno.github.io/kyverno/"
  chart      = "kyverno"
  version    = "3.3.2"
  namespace  = "kyverno"
  create_namespace = true

  values = [<<EOF
installCRDs: true
replicaCount: 1
backgroundScanning:
  enabled: true
  enforcement: "Audit"
policy:
  asBackend: false
EOF
  ]

  depends_on = [azurerm_kubernetes_cluster.aks]
}

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
  value       = azurerm_storage_share.openclaw.name
}

output "argocd_url" {
  description = "ArgoCD URL (Entra/OIDC login — register app first, see README)"
  value       = "https://argocd.claw-code.internal"
}

output "kubeconfig" {
  description = "Raw kubeconfig for the AKS cluster"
  value       = azurerm_kubernetes_cluster.aks.kube_config_raw
  sensitive   = true
}

output "deploy_identity_client_id" {
  description = "Client ID of the github-claw-code MI (used for Entra app registration OIDC clientID)"
  value       = data.azurerm_user_assigned_identity.deploy_identity.client_id
}

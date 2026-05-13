terraform {
  required_version = ">= 1.3"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15"
    }
  }

  backend "azurerm" {
    resource_group_name  = "terraform"
    storage_account_name = "mytofustates"
    container_name      = "claw-code"
    key                 = "dev.tfstate"
    use_azuread_auth    = true
  }
}

provider "azurerm" {
  features {}
  use_oidc = true
}

provider "azuread" {
  use_oidc = true
}

provider "kubernetes" {
  host                   = azurerm_kubernetes_cluster.aks.kube_config.0.host
  client_certificate     = base64decode(azurerm_kubernetes_cluster.aks.kube_config.0.client_certificate)
  client_key             = base64decode(azurerm_kubernetes_cluster.aks.kube_config.0.client_key)
  cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.aks.kube_config.0.cluster_ca_certificate)
}

provider "helm" {
  kubernetes {
    host                   = azurerm_kubernetes_cluster.aks.kube_config.0.host
    client_certificate     = base64decode(azurerm_kubernetes_cluster.aks.kube_config.0.client_certificate)
    client_key             = base64decode(azurerm_kubernetes_cluster.aks.kube_config.0.client_key)
    cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.aks.kube_config.0.cluster_ca_certificate)
  }
}

locals {
  resource_group_name = "claw-code"
  cluster_name        = var.cluster_name
  location            = var.location
  storage_account_name = var.storage_account_name
  namespace           = var.namespace
  aks_version         = "1.31"
  # Entra tenant ID for ArgoCD OIDC — replace with your actual tenant ID
  # or pass via TF_VAR_entra_tenant_id in the workflow.
  entra_tenant_id     = "1e1e851f-618f-40d4-9c2d-45355ad039a9"
}

# Get existing resource group
data "azurerm_resource_group" "rg" {
  name = local.resource_group_name
}

# Get the deploy managed identity (github-claw-code)
data "azurerm_user_assigned_identity" "deploy_identity" {
  name                = "github-claw-code"
  resource_group_name = data.azurerm_resource_group.rg.name
}

data "azuread_service_principal" "deploy_identity_principal" {
  client_id = data.azurerm_user_assigned_identity.deploy_identity.client_id
}

# Assign Owner to deploy identity so it can manage AKS resources
resource "azuread_service_principal_role_assignment" "deploy_identity_owner" {
  role_definition_id = "8e3af841-a98f-49bf-91df-df956c3c9783"  # Owner
  principal_id       = data.azurerm_user_assigned_identity.deploy_identity.principal_id
  principal_type     = "ServicePrincipal"
  scope              = data.azurerm_resource_group.rg.id
}

# Create storage account for PV (Azure Files) — name from TF var (clwcodecodev)
resource "azurerm_storage_account" "pv" {
  name                     = local.storage_account_name
  resource_group_name      = data.azurerm_resource_group.rg.name
  location                 = data.azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  tags = {
    environment = var.env
    project     = "claw-code"
  }
}

# Create a file share in the storage account (for K8s PV)
resource "azurerm_storage_share" "openclaw" {
  name = "openclaw"
  storage_account_id = azurerm_storage_account.pv.id
  quota_mb           = 51200  # 50GB
}

# Generate storage account key (used by K8s PV secret)
resource "azurerm_storage_account_primary_access_key" "pv" {
  storage_account_id = azurerm_storage_account.pv.id
}

# Create the AKS cluster
resource "azurerm_kubernetes_cluster" "aks" {
  name                = local.cluster_name
  resource_group_name  = data.azurerm_resource_group.rg.name
  location            = data.azurerm_resource_group.rg.location
  dns_prefix          = "claw-code"
  kubernetes_version  = local.aks_version
  sku_tier            = "Free"

  default_node_pool {
    name                = "default"
    vm_size            = var.node_size
    node_count         = var.node_count
    os_disk_size_gb    = 30
    os_disk_type       = "Managed"
    type               = "VirtualMachineScaleSets"
    availability_zones = ["1", "2", "3"]
  }

  identity {
    type = "SystemAssigned"
  }

  azure_active_directory_role_based_access_control {
    managed               = true
    azure_rbac_enabled     = true
    admin_group_object_ids = []  # Add your Entra group's Object ID here for cluster admin access
  }

  key_vault_secrets_provider {
    secret_rotation_enabled = false
  }

  oms_agent {
    log_analytics_workspace_id = azurerm_log_analytics_workspace.aks.id
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

# Log Analytics workspace for AKS monitoring
resource "azurerm_log_analytics_workspace" "aks" {
  name                = "claw-code-aks-la"
  location            = data.azurerm_resource_group.rg.location
  resource_group_name = data.azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

# Kubernetes RBAC: bind the deploy identity to cluster-admin (for GitHub Actions ops)
resource "kubernetes_cluster_role_binding" "deploy_identity_cluster_admin" {
  metadata {
    name = "deploy-identity-cluster-admin"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "cluster-admin"
  }
  subject {
    kind      = "ServiceAccount"
    name      = "deploy-identity"
    namespace = "kube-system"
  }
}

resource "kubernetes_service_account" "deploy_identity" {
  metadata {
    name      = "deploy-identity"
    namespace = "kube-system"
    annotations = {
      "azure.workload.identity/client-id" = data.azurerm_user_assigned_identity.deploy_identity.client_id
    }
  }
  mount_path = "/var/run/secrets/azure"
}

# Grant the deploy identity storage account contributor role
resource "azurerm_role_assignment" "deploy_identity_storage_contributor" {
  scope                    = azurerm_storage_account.pv.id
  role_definition_name     = "Storage Account Contributor"
  principal_id             = data.azurerm_user_assigned_identity.deploy_identity.principal_id
}

# =============================================================================
# ArgoCD Installation via Helm — Entra/OIDC-only auth (no local passwords)
# =============================================================================
# Prerequisites for Entra/OIDC login:
# 1. Register an Entra application at portal.azure.com → App registrations.
#    Set the redirect URI to: https://argocd.claw-code.internal/auth/callback
# 2. Add the "ArgoCD" API scope under Expose an API (or use any valid scope).
# 3. Create a client secret; add AZ_CLIENT_SECRET to GitHub Actions secrets.
# 4. Update argocd.oidc.config.clientSecret to use ${AZ_CLIENT_SECRET}
#    and pass TF_VAR_entra_client_secret in the workflow.
resource "helm_release" "argocd" {
  name       = "argocd"
  repository = "https://argoproj.github.io/argocd-helm/"
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
      clientID: ${data.azurerm_user_assigned_identity.deploy_identity.client_id}
      clientSecret: ${AZ_CLIENT_SECRET}
      requestedScopes:
        - openid
        - profile
        - email
        - offline_access
        - api://${data.azurerm_user_assigned_identity.deploy_identity.client_id}/ArgoCD
      requestedAudiences:
        - api://${data.azurerm_user_assigned_identity.deploy_identity.client_id}/ArgoCD

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

# Default-deny-all NetworkPolicy — blocks ALL ingress/egress by default.
# Add explicit allow policies for each required access pattern.
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
      - ports {
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
  description = "PV storage account name (from TF var — clwcodecodev, NOT mytofustates)"
  value       = azurerm_storage_account.pv.name
}

output "storage_account_key" {
  description = "PV storage account primary access key"
  value       = azurerm_storage_account_primary_access_key.pv.primary_key
  sensitive   = true
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
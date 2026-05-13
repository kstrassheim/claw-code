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
    container_name       = "claw-code"
    key                  = "dev.tfstate"
    use_azuread_auth     = true
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
}

# Get existing resource group
data "azurerm_resource_group" "rg" {
  name = local.resource_group_name
}

# Get the deploy managed identity (github-claw-code or github-claw-code-dev)
data "azurerm_user_assigned_identity" "deploy_identity" {
  name                = "github-claw-code"
  resource_group_name = data.azurerm_resource_group.rg.name
}

data "azuread_service_principal" "deploy_identity_principal" {
  client_id = data.azurerm_user_assigned_identity.deploy_identity.client_id
}

# Assign Owner to deploy identity so it can manage AKS resources
# (The user said Contributor access is already there, but Owner is needed for some AKS operations)
resource "azuread_service_principal_role_assignment" "deploy_identity_owner" {
  role_definition_id = "8e3af841-a98f-49bf-91df-df956c3c9783"  # Owner
  principal_id       = data.azurerm_user_assigned_identity.deploy_identity.principal_id
  principal_type     = "ServicePrincipal"
  scope              = data.azurerm_resource_group.rg.id
}

# Create storage account for PV (Azure Files)
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
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = data.azurerm_resource_group.rg.location
  dns_prefix          = "claw-code"
  kubernetes_version  = local.aks_version
  sku_tier            = "Free"  # Use Paid for production (required for some features)

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
    managed                = true
    azure_rbac_enabled      = true
    admin_group_object_ids  = []  # Add your group's object ID here for cluster admin access
  }

  # Enable keyvault-secrets-store CSI driver for Azure Key Vault provider
  key_vault_secrets_provider {
    secret_rotation_enabled = false
  }

  # oms_agent for Azure Monitor
  oms_agent {
    log_analytics_workspace_id = azurerm_log_analytics_workspace.aks.id
  }

  # Network profile (Azure CNI)
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

# Grant the deploy identity storage account contributor role (it needs this to create PVs)
resource "azurerm_role_assignment" "deploy_identity_storage_contributor" {
  scope              = azurerm_storage_account.pv.id
  role_definition_name = "Storage Account Contributor"
  principal_id       = data.azurerm_user_assigned_identity.deploy_identity.principal_id
}

# =============================================================================
# ArgoCD Installation via Helm
# =============================================================================
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
  secret:
    # Initial admin password - CHANGE THIS after first login
    # Password: admin
    argocdServerAdminPassword: "$2a$10$rSzGhJNKh6IJNiKqW0P3/.nqFYmQbN0rF5N3P0vZJqN2k7M9QfXLi"  # admin
  rbac:
    policy.default: "role:readonly"
    # Allow admins to manage applications
    policy.csv: |
      g, system:authenticated, role:readonly
      g, argocd-admins, role:admin
  # OIDC configuration (Entra ID)
  cmp:
    create: true

# Disable Dex (use direct Entra/OIDC instead)
dex:
  enabled: false

# Disable notifications (not needed)
notifications:
  enabled: false
EOF
  ]

  set {
    name  = "server.extraArgs"
    value = "--insecure"
  }

  set {
    name  = "server.replicas"
    value = "1"
  }

  set {
    name  = "controller.replicas"
    value = "1"
  }

  set {
    name  = "repoServer.replicas"
    value = "1"
  }

  depends_on = [azurerm_kubernetes_cluster.aks]
}

# =============================================================================
# Kyverno Installation via Helm with default-deny-all policy
# =============================================================================
resource "helm_release" "kyverno" {
  name       = "kyverno"
  repository = "https://kyverno.github.io/kyverno/"
  chart      = "kyverno"
  version    = "3.3.2"
  namespace  = "kyverno"
  create_namespace = true

  values = [<<EOF
# Install CRDs
installCRDs: true

# Replicas
 replicaCount: 1

# Background scanning (report-only mode, doesn't block)
 backgroundScanning:
   enabled: true
   enforcement: "Audit"

# Audit mode - doesn't block, just reports violations
# Change to "Enforce" to actually enforce policies
policy:
  asBackend: false
EOF
  ]

  depends_on = [azurerm_kubernetes_cluster.aks]
}

# Default-deny-all NetworkPolicy
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

# Allow DNS egress for all pods (required for cluster operation)
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
  description = "PV storage account name"
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
  description = "ArgoCD URL"
  value       = "https://argocd.claw-code.internal"
}

output "kubeconfig" {
  description = "Raw kubeconfig for the AKS cluster"
  value       = azurerm_kubernetes_cluster.aks.kube_config_raw
  sensitive   = true
}
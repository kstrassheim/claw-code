# =============================================================================
# The planning store.
#
# The bot records what it did — estimates, solver runs, deliveries — and the
# reports read it back. `builder/planning_store.py` carries two backends in one
# file and chooses between them from the environment; this deployment provides
# the Cosmos one, so no MongoDB is deployed here.
#
# NO KEYS ANYWHERE. `local_authentication_disabled` turns off the account keys
# entirely, so the only way in is Azure AD. The pod gets there with workload
# identity: its ServiceAccount token is exchanged for an AD token through the
# federated credential below, and the data-plane role assignments say what that
# identity may do. Nothing has to be sealed, rotated, or kept out of git,
# because there is no secret to keep.
# =============================================================================

# The identity the pod runs as. Separate from the kubelet identity on purpose:
# this one is scoped to reading and writing planning documents and nothing else.
resource "azurerm_user_assigned_identity" "pod" {
  name                = "${var.app_name}-pod-${var.env}"
  resource_group_name = data.azurerm_resource_group.rg.name
  location            = local.location
}

# Binds the Kubernetes ServiceAccount to the identity above. `subject` must
# match the pod's ServiceAccount exactly — namespace and name — or the token
# exchange fails with a mismatch that reads like a permissions error.
resource "azurerm_federated_identity_credential" "pod" {
  name = "${var.app_name}-workload"
  # resource_group_name is deprecated on this resource and goes away in the
  # next major provider version; the identity id already scopes it.
  user_assigned_identity_id = azurerm_user_assigned_identity.pod.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.aks.oidc_issuer_url
  subject                   = "system:serviceaccount:${local.namespace}:${var.app_name}"
}

resource "azurerm_cosmosdb_account" "planning" {
  name = var.cosmos_account_name

  # NOT local.location — see var.cosmos_location. West Europe refuses new
  # accounts under capacity pressure, and no setting here can satisfy it.
  location            = var.cosmos_location
  resource_group_name = data.azurerm_resource_group.rg.name

  offer_type = "Standard"
  kind       = "GlobalDocumentDB"

  # See the header: AD only, no account keys to leak. Spelled as the positive
  # flag because `local_authentication_disabled` is deprecated and goes away in
  # azurerm v5 — the double negative was also the harder one to read correctly.
  local_authentication_enabled = false

  # Serverless. The planning store writes a handful of small documents per
  # tick; provisioned throughput would bill for capacity that is idle almost
  # all of the time.
  capabilities {
    name = "EnableServerless"
  }

  # Session is the right consistency for this: a run reads back what it just
  # wrote, and nothing else needs a global ordering guarantee.
  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = var.cosmos_location
    failover_priority = 0
    zone_redundant    = false
  }
}

resource "azurerm_cosmosdb_sql_database" "planning" {
  name                = "planning"
  resource_group_name = data.azurerm_resource_group.rg.name
  account_name        = azurerm_cosmosdb_account.planning.name
}

resource "azurerm_cosmosdb_sql_container" "planning" {
  name                = "planning"
  resource_group_name = data.azurerm_resource_group.rg.name
  account_name        = azurerm_cosmosdb_account.planning.name
  database_name       = azurerm_cosmosdb_sql_database.planning.name

  partition_key_paths   = ["/pk"]
  partition_key_version = 2

  # Documents are kept until something deletes them. The store's ids are
  # deterministic and every write is an upsert, so nothing accumulates by
  # accident and an expiry would silently drop history the reports read.
  default_ttl = -1
}

# -----------------------------------------------------------------------------
# Data-plane RBAC.
#
# These are Cosmos' OWN role definitions, not Azure resource roles: the two
# well-known GUIDs are the built-in Data Reader (...0001) and Data Contributor
# (...0002). An Owner on the subscription still cannot read a document without
# one of these.
#
# `random_uuid` because the assignment NAME must be a GUID and must be stable
# across applies — deriving it from the principal would recreate the assignment
# whenever the identity was replaced.
# -----------------------------------------------------------------------------

resource "random_uuid" "cosmos_pod_contributor" {}

# The pod writes and reads planning documents, scoped to the one container.
resource "azurerm_cosmosdb_sql_role_assignment" "pod_data_contributor" {
  name                = random_uuid.cosmos_pod_contributor.result
  resource_group_name = data.azurerm_resource_group.rg.name
  account_name        = azurerm_cosmosdb_account.planning.name

  scope              = "${azurerm_cosmosdb_account.planning.id}/dbs/${azurerm_cosmosdb_sql_database.planning.name}/colls/${azurerm_cosmosdb_sql_container.planning.name}"
  role_definition_id = "${azurerm_cosmosdb_account.planning.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id       = azurerm_user_assigned_identity.pod.principal_id
}

resource "random_uuid" "cosmos_pod_reader" {}

# Account-level read as well: the client resolves the database and container
# before it can address a document, and that metadata read is refused by a
# container-scoped assignment alone.
resource "azurerm_cosmosdb_sql_role_assignment" "pod_account_reader" {
  name                = random_uuid.cosmos_pod_reader.result
  resource_group_name = data.azurerm_resource_group.rg.name
  account_name        = azurerm_cosmosdb_account.planning.name

  scope              = azurerm_cosmosdb_account.planning.id
  role_definition_id = "${azurerm_cosmosdb_account.planning.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001"
  principal_id       = azurerm_user_assigned_identity.pod.principal_id
}

resource "random_uuid" "cosmos_admins_contributor" {}

# Humans in the AKS admin group, so the store can be inspected and repaired
# without turning the account keys back on.
resource "azurerm_cosmosdb_sql_role_assignment" "admins_data_contributor" {
  name                = random_uuid.cosmos_admins_contributor.result
  resource_group_name = data.azurerm_resource_group.rg.name
  account_name        = azurerm_cosmosdb_account.planning.name

  scope              = azurerm_cosmosdb_account.planning.id
  role_definition_id = "${azurerm_cosmosdb_account.planning.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id       = data.azuread_group.aks_admin.object_id
}

resource "random_uuid" "cosmos_deploy_contributor" {}

# The deploy identity, so a pipeline can seed or migrate the container.
resource "azurerm_cosmosdb_sql_role_assignment" "deploy_data_contributor" {
  name                = random_uuid.cosmos_deploy_contributor.result
  resource_group_name = data.azurerm_resource_group.rg.name
  account_name        = azurerm_cosmosdb_account.planning.name

  scope              = azurerm_cosmosdb_account.planning.id
  role_definition_id = "${azurerm_cosmosdb_account.planning.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id       = data.azurerm_user_assigned_identity.deploy_identity.principal_id
}

# -----------------------------------------------------------------------------
# What the deploy needs to wire the pod up.
# -----------------------------------------------------------------------------

output "pod_identity_client_id" {
  description = "Client ID of the pod's workload identity. The deploy puts this on the ServiceAccount as azure.workload.identity/client-id."
  value       = azurerm_user_assigned_identity.pod.client_id
}

output "planning_cosmos_endpoint" {
  description = "Endpoint the planning store connects to (PLANNING_COSMOS_ENDPOINT)."
  value       = azurerm_cosmosdb_account.planning.endpoint
}

output "planning_cosmos_database" {
  value = azurerm_cosmosdb_sql_database.planning.name
}

output "planning_cosmos_container" {
  value = azurerm_cosmosdb_sql_container.planning.name
}

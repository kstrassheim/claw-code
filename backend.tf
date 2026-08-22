# =============================================================================
# OpenTofu core: version constraints, providers, remote state, and the
# encryption that wraps it.
#
# THIS PROJECT IS OPENTOFU ONLY from here on. The encryption block below and
# the variable reference in `backend` are both early-evaluation features that
# HashiCorp Terraform cannot parse — running `terraform` against this
# directory fails, and running it against this STATE cannot decrypt it. The CI
# workflows use opentofu/setup-opentofu accordingly.
#
# State lives in the mytofustates storage account under Entra auth (no keys),
# in a container named after the project.
# =============================================================================
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
      version = "~> 3.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    # For the Cosmos data-plane role assignment names, which must be GUIDs and
    # must stay stable across applies.
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  backend "azurerm" {
    resource_group_name  = "terraform"
    storage_account_name = "mytofustates"
    container_name       = var.app_name
    key                  = "dev.tfstate"
    use_azuread_auth     = true
  }

  # ---------------------------------------------------------------------------
  # State encryption, matching claw-mock. The data key that encrypts the state
  # is generated per run and wrapped with the RSA key named `claw-code` in the
  # kv-mytofustates Key Vault (envelope encryption) — the key material never
  # leaves the vault.
  #
  # `vault_key_name` is var.app_name, so the key object and the project name
  # are the same string by construction and cannot drift apart.
  # ---------------------------------------------------------------------------
  encryption {
    key_provider "azure_vault" "state" {
      vault_uri      = "https://kv-mytofustates.vault.azure.net"
      vault_key_name = var.app_name
      key_length     = 32

      # These MUST be block arguments. The key provider does not read
      # ARM_USE_OIDC / ARM_CLIENT_ID / ARM_TENANT_ID from the environment the
      # way the backend and the azurerm provider do; setting those has no
      # effect on it and it falls through to AzureCLICredential, which then
      # fails with "Please specify only one of subscription and tenant".
      use_oidc  = var.use_oidc
      use_cli   = !var.use_oidc
      client_id = var.arm_client_id
      tenant_id = var.arm_tenant_id
    }

    method "aes_gcm" "state" {
      keys = key_provider.azure_vault.state
    }

    state {
      method = method.aes_gcm.state

      # MIGRATION ONLY — REMOVE AFTER THE FIRST SUCCESSFUL APPLY.
      #
      # This project's existing state is PLAINTEXT. An empty fallback tells
      # OpenTofu to read unencrypted state when it cannot decrypt, so the first
      # run can read what is there and write it back encrypted. Without it the
      # first `tofu plan` fails outright and the migration cannot start.
      #
      # It must not stay. While it is here, a state that silently reverted to
      # plaintext would still be accepted, and the encryption would be a
      # comment rather than a guarantee.
      fallback {}
    }

    plan {
      method = method.aes_gcm.state
    }
  }
}

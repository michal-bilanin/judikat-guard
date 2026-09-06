# Judikát Guard on the Azure free account. See infra/azure/README.md.
#
# One VM serving the built frontend and reverse-proxying /api to the Spring Boot jar, plus a
# managed PostgreSQL Flexible Server. No container image and no CI pipeline: CLAUDE.md rules
# them out, and a jar under systemd needs neither.

terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {}

  subscription_id = var.subscription_id

  # The single most likely cause of a failed first apply on a fresh subscription.
  #
  # azurerm 5.0 (2026-07-27) changed `resource_provider_registrations` to default to "none";
  # before that it registered a large legacy set for you. A brand-new free subscription has
  # nothing registered, so without this list the first apply dies with
  # MissingSubscriptionRegistration. `skip_provider_registration` was removed in the same
  # release, so older examples that set it will not even `terraform init`.
  resource_provider_registrations = "none"
  resource_providers_to_register = [
    "Microsoft.Compute",
    "Microsoft.Network",
    "Microsoft.DBforPostgreSQL",
  ]
}

# Postgres server names are globally unique and the public IP's domain label must be unique
# within its region, so both carry a suffix. Without it the first apply collides with anyone
# else who ever ran this file.
resource "random_string" "suffix" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

# Generated rather than supplied: one less secret for a human to invent, store badly, and
# commit. Read it with `terraform output -raw postgres_password`.
resource "random_password" "postgres" {
  length           = 32
  special          = true
  override_special = "!#$%*()-_=+[]{}<>:?"
}

resource "azurerm_resource_group" "jg" {
  name     = var.resource_group_name
  location = var.location

  tags = local.tags
}

locals {
  suffix = random_string.suffix.result

  # `curl -4 -s https://ifconfig.me` prints a bare address, so that is what people paste.
  # Normalise here rather than making the reader convert it, and use this everywhere instead
  # of var.ssh_source_cidr.
  ssh_cidr = strcontains(var.ssh_source_cidr, "/") ? var.ssh_source_cidr : "${var.ssh_source_cidr}/32"

  tags = {
    project    = "judikat-guard"
    managed_by = "terraform"
  }

  # Everything the app needs on disk, in the layout the app expects. See vm.tf.
  app_dir = "/opt/judikat-guard"

  # The hostname Caddy will request a certificate for.
  fqdn = azurerm_public_ip.jg.fqdn
}

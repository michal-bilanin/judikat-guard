# Azure Database for PostgreSQL Flexible Server.
#
# Note for anyone adapting an older example: every azurerm "single server" Postgres resource
# (azurerm_postgresql_server, azurerm_postgresql_database, azurerm_postgresql_firewall_rule)
# was deleted in provider 5.0. Only the *_flexible_server_* family exists now.
resource "azurerm_postgresql_flexible_server" "jg" {
  name                = "jg-pg-${local.suffix}"
  resource_group_name = azurerm_resource_group.jg.name
  location            = azurerm_resource_group.jg.location
  tags                = local.tags

  version = "16"

  # The free account's included tier: 750 hours/month of Burstable B1MS, which covers one
  # server running continuously.
  sku_name = "B_Standard_B1ms"

  storage_mb   = var.postgres_storage_mb
  storage_tier = "P4"

  # Left at the provider default of false, stated explicitly because the consequence is
  # invisible: if storage ever auto-grows past 32 GB the server leaves the free allowance and
  # starts billing, and storage cannot be shrunk back afterwards.
  auto_grow_enabled = false

  # The free account includes 32 GB of backup storage. Seven days is the minimum and stays
  # inside it; the corpus is reproducible from data/raw anyway.
  backup_retention_days        = 7
  geo_redundant_backup_enabled = false

  administrator_login    = "judikat"
  administrator_password = random_password.postgres.result

  public_network_access_enabled = true

  lifecycle {
    # The zone Azure picks at create time is not stable in the API's responses, and a diff on
    # it would propose destroying the database.
    ignore_changes = [zone]
  }
}

# Azure creates only postgres, azure_maintenance and azure_sys. The application connects to a
# database called "judikat", and a custom-format pg_dump carries no CREATE DATABASE — so
# without this resource the first API boot fails to connect and `make seed-remote` has
# nothing to restore into.
resource "azurerm_postgresql_flexible_server_database" "judikat" {
  name      = "judikat"
  server_id = azurerm_postgresql_flexible_server.jg.id
  collation = "en_US.utf8"
  charset   = "utf8"

  lifecycle {
    # Dropping a database because a collation string drifted is not a trade worth making.
    prevent_destroy = true
  }
}

# Only the VM may connect. Microsoft's own guidance for this shape is to use the public IP
# tied to the virtual machine, which is why the address is static — and it is why the broad
# "allow all Azure services" 0.0.0.0 rule is neither present nor needed here. That rule
# admits every Azure tenant, not just yours.
resource "azurerm_postgresql_flexible_server_firewall_rule" "vm" {
  name             = "allow-app-vm"
  server_id        = azurerm_postgresql_flexible_server.jg.id
  start_ip_address = azurerm_public_ip.jg.ip_address
  end_ip_address   = azurerm_public_ip.jg.ip_address
}

# Your workstation, so `make seed-remote` can restore the dump and psql can reach the
# database for inspection. Same CIDR as the SSH rule, and the same caveat: a changing home
# address needs a re-apply.
resource "azurerm_postgresql_flexible_server_firewall_rule" "operator" {
  name             = "allow-operator"
  server_id        = azurerm_postgresql_flexible_server.jg.id
  start_ip_address = cidrhost(local.ssh_cidr, 0)
  end_ip_address   = cidrhost(local.ssh_cidr, -1)
}

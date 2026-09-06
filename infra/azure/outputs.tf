output "url" {
  description = "The deployed page. HTTPS works once Caddy has fetched a certificate."
  value       = "https://${azurerm_public_ip.jg.fqdn}"
}

output "fqdn" {
  description = "Public hostname of the VM."
  value       = azurerm_public_ip.jg.fqdn
}

output "vm_ip" {
  description = "Static public IP. Also the only address the database firewall admits."
  value       = azurerm_public_ip.jg.ip_address
}

output "ssh" {
  description = "Ready-made SSH command."
  value       = "ssh azureuser@${azurerm_public_ip.jg.fqdn}"
}

output "postgres_host" {
  value = azurerm_postgresql_flexible_server.jg.fqdn
}

output "postgres_user" {
  value = azurerm_postgresql_flexible_server.jg.administrator_login
}

output "postgres_password" {
  description = "Generated. Read with: terraform output -raw postgres_password"
  value       = random_password.postgres.result
  sensitive   = true
}

# What the application needs in its EnvironmentFile. sslmode=require is not optional: Azure
# Flexible Server rejects unencrypted connections, and the JDBC driver does not infer it.
output "jdbc_url" {
  description = "JG_JDBC_URL for the app and for make seed-remote."
  value       = "jdbc:postgresql://${azurerm_postgresql_flexible_server.jg.fqdn}:5432/${azurerm_postgresql_flexible_server_database.judikat.name}?sslmode=require"
}

output "psql_url" {
  description = "libpq URL for psql and pg_restore."
  value       = "postgresql://${azurerm_postgresql_flexible_server.jg.administrator_login}:${urlencode(random_password.postgres.result)}@${azurerm_postgresql_flexible_server.jg.fqdn}:5432/${azurerm_postgresql_flexible_server_database.judikat.name}?sslmode=require"
  sensitive   = true
}

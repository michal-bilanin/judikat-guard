resource "azurerm_linux_virtual_machine" "jg" {
  name                = "jg-vm"
  resource_group_name = azurerm_resource_group.jg.name
  location            = azurerm_resource_group.jg.location
  size                = var.vm_size

  admin_username                  = "azureuser"
  disable_password_authentication = true

  network_interface_ids = [azurerm_network_interface.jg.id]

  tags = local.tags

  admin_ssh_key {
    username   = "azureuser"
    public_key = var.ssh_public_key
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"

    # 64 GiB is the P6 tier, and the free account includes two P6 Premium SSDs for 12
    # months. A smaller disk is not cheaper here: it drops to P4, which the grant does not
    # cover, so it bills while this does not.
    disk_size_gb = 64
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }

  # Must be base64. Editing cloud-init.yaml REPLACES the VM — the database is a separate
  # resource and survives, but the app has to be re-deployed afterwards.
  custom_data = base64encode(templatefile("${path.module}/cloud-init.yaml", {
    fqdn    = azurerm_public_ip.jg.fqdn
    app_dir = local.app_dir
  }))

  # The FQDN is baked into the Caddyfile, so the address must exist before the VM renders
  # its cloud-init. Terraform infers this through azurerm_public_ip.jg.fqdn above; the NSG
  # association is the one that needs saying out loud, because nothing else references it
  # and a VM that boots before its rules exist cannot fetch a certificate.
  depends_on = [azurerm_network_interface_security_group_association.jg]
}

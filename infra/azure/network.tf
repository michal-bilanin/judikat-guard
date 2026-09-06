resource "azurerm_virtual_network" "jg" {
  name                = "jg-vnet"
  address_space       = ["10.20.0.0/16"]
  location            = azurerm_resource_group.jg.location
  resource_group_name = azurerm_resource_group.jg.name
  tags                = local.tags
}

resource "azurerm_subnet" "jg" {
  name                 = "jg-subnet"
  resource_group_name  = azurerm_resource_group.jg.name
  virtual_network_name = azurerm_virtual_network.jg.name
  address_prefixes     = ["10.20.1.0/24"]
}

# Static because a Standard SKU public IP has no Dynamic option, and because the address is
# the source of the Postgres firewall rule — a changing address would silently lock the app
# out of its own database.
#
# COST: this is the one resource here that is not in the free allowance. A static Standard
# IPv4 bills from creation whether or not the VM is running, roughly $3-4/month. It is the
# floor on what this deployment costs even while everything else is free.
resource "azurerm_public_ip" "jg" {
  name                = "jg-ip"
  location            = azurerm_resource_group.jg.location
  resource_group_name = azurerm_resource_group.jg.name
  allocation_method   = "Static"
  sku                 = "Standard"

  # Gives a real, resolvable hostname, which is what lets Caddy get a Let's Encrypt
  # certificate without owning a domain. cloudapp.azure.com is on the Public Suffix List, so
  # this label gets its own rate limit rather than sharing one with every other Azure VM.
  domain_name_label = "${var.dns_label}-${local.suffix}"

  tags = local.tags
}

# A Standard SKU public IP is closed to all inbound traffic by default — Azure's "secure by
# default" model means the NSG is not hardening, it is the only reason anything answers.
resource "azurerm_network_security_group" "jg" {
  name                = "jg-nsg"
  location            = azurerm_resource_group.jg.location
  resource_group_name = azurerm_resource_group.jg.name
  tags                = local.tags

  # Scoped to one address on purpose. This is the deploy path: `make deploy` pushes the jar
  # over SSH, so losing this rule means losing the ability to ship.
  security_rule {
    name                       = "ssh"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = local.ssh_cidr
    destination_address_prefix = "*"
  }

  # Open to the world because the demo is. Port 80 is not decoration: Caddy needs it for the
  # ACME HTTP-01 challenge, and it redirects to HTTPS afterwards.
  security_rule {
    name                       = "http"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "https"
    priority                   = 130
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }
}

resource "azurerm_network_interface" "jg" {
  name                = "jg-nic"
  location            = azurerm_resource_group.jg.location
  resource_group_name = azurerm_resource_group.jg.name
  tags                = local.tags

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.jg.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.jg.id
  }
}

resource "azurerm_network_interface_security_group_association" "jg" {
  network_interface_id      = azurerm_network_interface.jg.id
  network_security_group_id = azurerm_network_security_group.jg.id
}

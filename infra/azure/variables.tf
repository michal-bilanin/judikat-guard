variable "subscription_id" {
  type        = string
  description = <<-EOT
    The Azure subscription to deploy into. Required at plan/apply time by azurerm 5.x; it can
    also come from ARM_SUBSCRIPTION_ID or the az CLI's default subscription, but naming it
    here means a second subscription cannot be hit by accident.

    Find it with: az account show --query id -o tsv
  EOT
}

variable "resource_group_name" {
  type        = string
  description = "Everything lives in one group so `terraform destroy` is total."
  default     = "judikat-guard"
}

variable "location" {
  type        = string
  description = <<-EOT
    Azure region. Pick one near you and near the courts' users; westeurope is the obvious
    choice from Czechia. Note that azurerm 5.0 disabled location validation at plan time, so
    a typo here surfaces only when Azure rejects the apply.
  EOT
  default     = "westeurope"
}

variable "dns_label" {
  type        = string
  description = <<-EOT
    Leftmost label of the public hostname. The full name becomes
    <dns_label>-<suffix>.<location>.cloudapp.azure.com, which is a real DNS name that
    Let's Encrypt will issue a certificate for — so the demo gets HTTPS with no domain
    purchase and no DNS configuration.
  EOT
  default     = "judikat-guard"
}

variable "ssh_public_key" {
  type        = string
  description = <<-EOT
    Contents of your SSH public key, e.g. file("~/.ssh/id_ed25519.pub"). Password login is
    disabled on the VM, so losing this means losing the box.
  EOT
}

variable "ssh_source_cidr" {
  type        = string
  description = <<-EOT
    Who may reach port 22. Deliberately has no default: 0.0.0.0/0 would put an SSH port on
    the public internet for a demo box, and defaults are what people forget to change.

    Your current address: curl -s https://ifconfig.me
    Then set it as a /32, e.g. "203.0.113.7/32".

    A home IP usually changes. If SSH stops working, re-run apply with the new value — the
    NSG rule updates in place and does not touch the VM.
  EOT

  validation {
    condition     = can(cidrhost(var.ssh_source_cidr, 0))
    error_message = "ssh_source_cidr must be a CIDR block, e.g. 203.0.113.7/32."
  }
}

variable "vm_size" {
  type        = string
  description = <<-EOT
    Standard_B1s (1 vCPU, 1 GiB) is in the free account's 750 hours/month, which covers one
    VM running continuously. The API measured 180 MB RSS at -Xmx384m, so it fits — with a
    swapfile configured in cloud-init for headroom during startup and Flyway.

    Standard_B2ats_v2 and Standard_B2pts_v2 are also in the free allowance and are markedly
    roomier (2 vCPU, 1 GiB). Worth switching to if the box feels tight; the B-series v1 sizes
    are the older generation.
  EOT
  default     = "Standard_B1s"
}

variable "postgres_storage_mb" {
  type        = number
  description = <<-EOT
    32768 MB is exactly the free account's included PostgreSQL storage. The corpus restores
    to 529 MB, so this is ~60x headroom — do not raise it casually, because crossing 32 GB
    leaves the free allowance.

    Storage on Flexible Server can only ever grow, never shrink: raising this is a one-way
    door that also raises the bill after the free period.
  EOT
  default     = 32768
}

variable "budget_amount_eur" {
  type        = number
  description = <<-EOT
    Monthly spend that triggers an email. This is an ALERT, not a cap — Azure has no hard
    spending limit on pay-as-you-go, and the free account converts to pay-as-you-go once the
    30-day credit ends. Nothing here can stop a charge; it can only tell you about one.
  EOT
  default     = 5
}

variable "budget_alert_email" {
  type        = string
  description = "Where budget alerts go. Use the account's own address."
}

variable "budget_start_date" {
  type        = string
  description = <<-EOT
    First day of the current month, UTC, e.g. "2026-09-01T00:00:00Z".

    Azure rejects a start date outside the current time grain at create time, so a value
    hardcoded last month will fail today. It is immutable server-side, hence the
    ignore_changes in budget.tf: once created, leave it alone.
  EOT
}

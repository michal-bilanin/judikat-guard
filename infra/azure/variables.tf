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
    Azure region.

    NOT westeurope, and probably not germanywestcentral either. Azure closes regions to new
    subscriptions, and an apply into a closed one fails with "RequestDisallowedByAzure: The
    selected region is currently not accepting new customers"
    (https://aka.ms/locationineligible) — per resource, midway through, never at plan time.

    Eligibility is a property of YOUR subscription, not of this configuration. Measured on
    the subscription this was written for, the only European regions offering an unrestricted
    free-tier VM size were polandcentral and swedencentral; westeurope and germanywestcentral
    both carried a Location-type restriction. Yours may differ.

    Check before applying:  make tf-region-check LOC=polandcentral

    polandcentral is the default because it is the closest of the two to Czechia.

    Changing this after a successful apply REPLACES the resource group and everything in it,
    the database included. Choose once.

    Note also that azurerm 5.0 disabled location validation at plan time, so a typo here
    surfaces only when Azure rejects the apply.
  EOT
  default     = "polandcentral"
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
    Who may reach port 22, and who may reach the database. Deliberately has no default:
    0.0.0.0/0 would put an SSH port on the public internet for a demo box, and defaults are
    what people forget to change.

    Get it with:  curl -4 -s https://ifconfig.me

    Paste the result as-is. A bare address is normalised to /32; an explicit CIDR is taken as
    given, so "203.0.113.7" and "203.0.113.7/32" both work, and "203.0.113.0/24" widens it to
    an office range.

    IPv4 only. The -4 matters: on a dual-stack connection curl may hand back an IPv6 address,
    and Azure's PostgreSQL firewall rules take IPv4 addresses only — so an IPv6 value would
    pass here and then fail mid-apply, after the VM already exists.

    A home address usually changes. If SSH or `make seed-remote` stops working, re-run apply
    with the new value: both rules update in place and neither touches the VM.
  EOT

  validation {
    # Not `can(cidrhost(...))`: that accepts IPv6 too, and the failure would land at apply
    # time on the Postgres firewall rule rather than here. Bare addresses are accepted
    # because that is exactly what the documented curl command prints.
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}(/(3[0-2]|[12]?[0-9]))?$", var.ssh_source_cidr))
    error_message = <<-EOT
      ssh_source_cidr must be an IPv4 address or IPv4 CIDR — "203.0.113.7" or "203.0.113.7/32".
      Get yours with: curl -4 -s https://ifconfig.me
      (IPv6 is not supported: Azure's PostgreSQL firewall rules are IPv4-only.)
    EOT
  }
}

variable "vm_size" {
  type        = string
  description = <<-EOT
    Standard_B2ats_v2 (2 vCPU, 1 GiB) — in the free account's 750 hours/month, which covers
    one VM running continuously. The API measured 180 MB RSS at -Xmx384m, so it fits, with a
    swapfile from cloud-init for headroom during startup and Flyway.

    Not Standard_B1s, despite that being the size the free-tier documentation leads with.
    Measured on a real subscription, B1s carried a Location-type "NotAvailableForSubscription"
    restriction in every European region — it is the older B-series generation and Azure is
    steering new subscriptions off it. B2ats_v2 and B2pts_v2 are equally free and actually
    available.

    Whatever you set here, check it: make tf-region-check LOC=<region> SIZE=<size>
  EOT
  default     = "Standard_B2ats_v2"
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

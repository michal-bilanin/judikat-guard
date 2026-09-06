# An alert, not a cap.
#
# Azure has no hard spending limit on pay-as-you-go, and the free account moves onto
# pay-as-you-go once the 30-day credit ends. Nothing in this file can stop a charge. It
# sends email at 80% forecast and 100% actual, and that is the entire mechanism available.
#
# It also cannot see the bill that is most likely to surprise you: Gemini usage is Google's,
# invoiced by Google, and invisible to every Azure cost tool. See the exposure notes in
# infra/azure/README.md.
resource "azurerm_consumption_budget_resource_group" "jg" {
  name              = "jg-budget"
  resource_group_id = azurerm_resource_group.jg.id

  amount     = var.budget_amount_eur
  time_grain = "Monthly"

  time_period {
    start_date = var.budget_start_date
  }

  # Forecast first: it is the earlier of the two signals. Azure's anomaly detection is the
  # slower one — it trains on 60 days of history and runs 36 hours after the end of the UTC
  # day, so on a subscription that is days old it has nothing to say.
  notification {
    enabled        = true
    threshold      = 80
    threshold_type = "Forecasted"
    operator       = "GreaterThan"
    contact_emails = [var.budget_alert_email]
  }

  notification {
    enabled        = true
    threshold      = 100
    threshold_type = "Actual"
    operator       = "GreaterThan"
    contact_emails = [var.budget_alert_email]
  }

  lifecycle {
    # start_date is immutable server-side. Left alone it drifts out of the current time
    # grain and every subsequent plan proposes an impossible update.
    ignore_changes = [time_period[0].start_date]
  }
}

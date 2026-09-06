# Deploying to the Azure free account

One VM serving the built frontend and reverse-proxying `/api` to the Spring Boot jar, plus a
managed PostgreSQL Flexible Server. Terraform provisions; cloud-init builds the host;
`make deploy` ships the app. No container image and no CI pipeline — CLAUDE.md rules both
out, and a jar under systemd needs neither.

Verified against `hashicorp/azurerm` **5.4.0** and Terraform 1.16: `terraform validate`
passes and `terraform fmt` is clean. Nothing here has been applied against a real
subscription — see [What has not been tested](#what-has-not-been-tested).

## What it creates

| Resource | Free-tier standing |
|---|---|
| `Standard_B1s` VM, Ubuntu 24.04 | 750 h/month for 12 months — covers one VM running continuously |
| 64 GiB Premium SSD OS disk | The P6 tier; the free account includes two P6 disks for 12 months |
| PostgreSQL Flexible Server, `B_Standard_B1ms`, 32 GB | 750 h/month + 32 GB storage + 32 GB backup, 12 months |
| VNet, subnet, NIC, NSG | Free |
| **Static Standard public IPv4** | **Not free. Roughly $3–4/month, billed from creation.** |
| Consumption budget | Free |

The public IP is the honest floor: this deployment is not zero, it is a few euros a month,
and it bills whether or not the VM is running.

A 64 GiB disk is not a mistake — a *smaller* disk is the expensive choice. Drop to 30 GiB and
it becomes a P4, which the free grant does not cover, so it starts billing while the larger
one does not.

## Order of operations

```bash
cp infra/azure/terraform.tfvars.example infra/azure/terraform.tfvars
$EDITOR infra/azure/terraform.tfvars      # subscription id, SSH key, your IP, email
                                          # your IP: curl -4 -s https://ifconfig.me

make tf-region-check LOC=germanywestcentral   # do this FIRST — see below
make tf-init
make tf-apply                             # ~10 min, most of it the database

export GEMINI_API_KEY=...                 # else the proposition check answers 503
make deploy                               # build, ship, write the env file, start
make seed-remote                          # ~124 MB over your uplink, once
make deploy-smoke
```

`make tf-output` prints the URL. Certificates take a few seconds on first hit; `make
deploy-logs` tails the API.

Flyway runs on application startup, so the schema builds itself on first boot — there is no
separate migration step against the cloud database. `make seed-remote` therefore carries data
only.

## Pick a region and a VM size before you apply

Two separate things can refuse you, and neither implies the other. Measured on the
subscription this was written for:

| | `westeurope` | `polandcentral` |
|---|---|---|
| `Standard_B2ats_v2` available | yes | yes |
| Region accepting new resources | **no** | yes |
| `Standard_B1s` available | **no** | **no** |

So `make tf-region-check` runs two checks: a SKU query, and an actual create-and-delete of a
free VNet in the target region. Only the second catches `RequestDisallowedByAzure: The
selected region is currently not accepting new customers`, which is the failure that bites,
and it bites per resource midway through an apply rather than at plan time.

```bash
make tf-region-check LOC=polandcentral        # the default
make tf-region-check LOC=swedencentral
make tf-region-scan                           # every region where the size is unrestricted (slow)
```

**Not `Standard_B1s`**, despite the free-tier documentation leading with it. On a real
subscription it carried a Location-scoped `NotAvailableForSubscription` in every European
region — it is the older B-series generation. `Standard_B2ats_v2` (2 vCPU, 1 GiB) is equally
inside the 750 free hours and is actually obtainable, so it is the default here.

`polandcentral` is the default region: the closest to Czechia of the two that passed both
checks. Yours may differ — eligibility is a property of your subscription.

**Choose once.** Changing `location` after a successful apply replaces the resource group and
everything inside it, database included — an Azure resource group's region is fixed at
creation.

## Three things that would otherwise break it silently

**The fat jar does not contain `extract/patterns.toml` or `prompts/`.** `PatternSet` and
`PromptTemplate` read them from disk, walking up from the working directory, precisely so a
stale copy baked into a build cannot diverge from the file the Python pipeline uses. Deploy
only the jar and the API starts cleanly and then fails on the first request. `make deploy`
ships all three into `/opt/judikat-guard`, and the systemd unit sets `WorkingDirectory` there
so the upward walk finds them.

**azurerm 5.0 stopped registering resource providers.** The default changed from `legacy` to
`none` in July 2026, and `skip_provider_registration` was removed outright. On a brand-new
subscription the first apply fails with `MissingSubscriptionRegistration` unless the
providers are listed explicitly, which `main.tf` does. Any older example you copy will not
even `terraform init`.

**cloud-init must not write `/etc/caddy/Caddyfile` directly.** `write_files` runs before
`runcmd`, so the real path would hold an unowned file when the caddy package installs. dpkg
prompts for the conffile, finds no stdin on an unattended boot, and aborts configure — the
package lands in state `iU`, `postinst` never runs, the `caddy` user is never created, and
the service dies with `status=217/USER`. The apply succeeds, the API comes up on :8080, and
443 answers nothing, while `caddy validate` says the config is fine. Hit on the first real
deployment. The file is therefore staged at `/etc/caddy/Caddyfile.jg` and copied into place
after the package is configured.

If you meet this on an existing box, repair it without rebuilding:

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.jg
sudo rm -f /etc/caddy/Caddyfile
sudo DEBIAN_FRONTEND=noninteractive dpkg --configure -a
sudo cp /etc/caddy/Caddyfile.jg /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

## Exposure — read this before sharing the URL

The application has **no authentication**, and CLAUDE.md forbids adding any. That is a
reasonable rule for a read-only demo over public court decisions. It stops being obviously
reasonable once one endpoint spends money.

`POST /api/decisions/{ecli}/proposition-check` reads the whole cited decision — averaging
tens of thousands of characters — and sends it to Gemini on your free-tier quota. There is no
rate limit, no per-IP throttle and no budget cap anywhere in the codebase. An anonymous
visitor, a crawler, or a scanner following `/api/openapi` (which is public and hands over the
full endpoint list) can drain that quota, and the resulting bill is Google's — **invisible to
the Azure budget in `budget.tf`, which cannot see it and cannot stop it**.

Worth knowing about the shape of the cost: `PropositionService` reads the decision's
paragraphs *before* `PropositionChecker` consults the cache, so even a cache hit pays the
database read.

`make protect USER=demo PASS=...` puts basic auth on the two proposition-check paths at the
Caddy layer and leaves the traffic lights open. That is a gate in front of the application,
not authentication inside it, so it does not add user accounts, sessions, or an identity
model to the codebase — the thing the rule is protecting against. You may read the rule more
strictly than that; if so, the alternative is to keep the URL unshared, or to leave
`GEMINI_API_KEY` unset so the endpoint returns 503 and only the traffic lights work.

Either way it is your call, and it should be a deliberate one.

## Cost guardrails, and their limits

`budget.tf` emails at 80% forecast and 100% actual. It is an **alert, not a cap**: Azure has
no hard spending limit on pay-as-you-go, and the free account converts to pay-as-you-go when
the 30-day credit ends. Nothing in this repository can stop an Azure charge; it can only tell
you about one.

Two dates to put in a calendar: **30 days** after signup, when the $200 credit expires, and
**12 months**, when the free service allowances end and every resource here starts billing at
standard rates.

## Teardown

```bash
make tf-destroy
```

Removes everything, the database included — `prevent_destroy` guards the `judikat` database
resource against an accidental single-resource destroy, so if you genuinely want it gone,
remove that block first. Check the portal for a leftover `NetworkWatcherRG`, which Azure
creates automatically outside this resource group and Terraform therefore never deletes.

## What has not been tested

`terraform validate` and `terraform fmt` pass, the cloud-init parses, and the jar name and
payload layout were verified against a real local build. But **no `terraform apply` has been
run against a real subscription**, so first-apply failures — quota refusals on a new account,
a name collision the random suffix does not cover, an image SKU unavailable in your region —
would surface on your first run, not here.

The `psql_url` output embeds the generated password; `terraform output -raw psql_url` is safe
to pipe, but do not paste it into a shared terminal. The password also sits in cleartext in
`terraform.tfstate`, which is why `.gitignore` excludes state and tfvars.

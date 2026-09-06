SHELL := /bin/bash
.DEFAULT_GOAL := help

ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# --- local toolchain -------------------------------------------------------
# `make toolchain` drops a JDK 25 and a Maven into .tools/ so the project builds without
# touching the system package manager. If you already have a JDK 25 and a Maven on PATH,
# set JAVA_HOME and MVN yourself and skip it.
TOOLS      := $(ROOT)/.tools
JDK_VERSION := 25.0.4.1_1
JDK_TAG     := jdk-25.0.4.1%2B1
JDK_URL     := https://github.com/adoptium/temurin25-binaries/releases/download/$(JDK_TAG)/OpenJDK25U-jdk_x64_linux_hotspot_$(JDK_VERSION).tar.gz
MVN_VERSION := 3.9.16
MVN_URL     := https://repo1.maven.org/maven2/org/apache/maven/apache-maven/$(MVN_VERSION)/apache-maven-$(MVN_VERSION)-bin.tar.gz

ifneq ($(wildcard $(TOOLS)/jdk/bin/java),)
  export JAVA_HOME := $(TOOLS)/jdk
endif
MVN := $(if $(wildcard $(TOOLS)/maven/bin/mvn),$(TOOLS)/maven/bin/mvn,mvn)

# --- python ----------------------------------------------------------------
VENV   := $(ROOT)/pipeline/.venv
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
JG     := $(VENV)/bin/jg
PYTEST := $(VENV)/bin/pytest

# --- database --------------------------------------------------------------
# Port 55432 to stay clear of a system postgres on 5432.
export PGHOST     ?= localhost
export PGPORT     ?= 55432
export PGDATABASE ?= judikat
export PGUSER     ?= judikat
export PGPASSWORD ?= judikat
export JG_DB_URL  ?= postgresql://$(PGUSER):$(PGPASSWORD)@$(PGHOST):$(PGPORT)/$(PGDATABASE)

COURT ?= NSS

.PHONY: help toolchain up down psql migrate migrate-repair migrate-info clean-db api web web-install \
        venv crawl crawl-window load-us extract classify eval eval-extract test test-java \
        test-python fmt tf-init tf-plan tf-apply tf-output tf-destroy deploy deploy-env \
        seed-remote deploy-logs deploy-smoke protect unprotect

help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- toolchain -------------------------------------------------------------

toolchain: $(TOOLS)/jdk/bin/java $(TOOLS)/maven/bin/mvn ## Install JDK 25 + Maven into .tools/
	@$(TOOLS)/jdk/bin/java -version

$(TOOLS)/jdk/bin/java:
	@mkdir -p $(TOOLS)
	curl -fsSL -o $(TOOLS)/jdk.tar.gz "$(JDK_URL)"
	tar xzf $(TOOLS)/jdk.tar.gz -C $(TOOLS) && rm -f $(TOOLS)/jdk.tar.gz
	ln -sfn "$$(basename $$(ls -d $(TOOLS)/jdk-25*/))" $(TOOLS)/jdk

$(TOOLS)/maven/bin/mvn:
	@mkdir -p $(TOOLS)
	curl -fsSL -o $(TOOLS)/maven.tar.gz "$(MVN_URL)"
	tar xzf $(TOOLS)/maven.tar.gz -C $(TOOLS) && rm -f $(TOOLS)/maven.tar.gz
	ln -sfn "apache-maven-$(MVN_VERSION)" $(TOOLS)/maven

# --- database --------------------------------------------------------------

up: ## Start Postgres and wait for it
	docker compose up -d
	@echo -n "waiting for postgres"
	@for i in $$(seq 1 60); do \
	  if docker compose exec -T postgres pg_isready -U $(PGUSER) -d $(PGDATABASE) >/dev/null 2>&1; then \
	    echo " ok"; exit 0; fi; echo -n .; sleep 1; done; \
	echo " timed out"; exit 1

down: ## Stop Postgres (keeps the volume)
	docker compose down

clean-db: ## Stop Postgres and delete the volume
	docker compose down -v

psql: ## Open a psql shell
	docker compose exec -it postgres psql -U $(PGUSER) -d $(PGDATABASE)

migrate: ## Apply Flyway migrations
	$(MVN) -q -f api/pom.xml flyway:migrate

migrate-repair: ## Re-checksum applied migrations (needed once, after V1 lost its vector column)
	$(MVN) -q -f api/pom.xml flyway:repair

migrate-info: ## Show migration status
	$(MVN) -q -f api/pom.xml flyway:info

# --- services --------------------------------------------------------------

api: ## Run the Spring Boot API on :8080
	$(MVN) -f api/pom.xml spring-boot:run

web: web-install ## Run the Vite dev server on :5173
	cd web && npm run dev

web-install: web/node_modules ## Install web dependencies
web/node_modules: web/package.json
	cd web && npm install
	@touch web/node_modules

# --- pipeline --------------------------------------------------------------

venv: $(JG) ## Create the Python venv and install the pipeline
$(JG): pipeline/pyproject.toml
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e "pipeline[dev]"
	@touch $(JG)

crawl: venv ## Crawl one court, e.g. make crawl COURT=NSS
	$(JG) crawl $(COURT)

# A bulk load is a long job: one request per second per host means a full year of NSS takes
# roughly two hours. Everything is cached under data/raw/, so an interrupted run resumes and
# a repeat run issues no network requests at all.
crawl-window: venv ## Bulk-crawl a date window, e.g. make crawl-window SINCE=2023-01-01 UNTIL=2023-12-31
	@test -n "$(SINCE)" -a -n "$(UNTIL)" || { \
	  echo "usage: make crawl-window SINCE=YYYY-MM-DD UNTIL=YYYY-MM-DD"; exit 2; }
	$(PY) scripts/bulk_crawl.py $(SINCE) $(UNTIL)

# The Constitutional Court is not enumerated: NALUS is addressed by document key, and the
# crawled corpus already says which ÚS decisions it cites. This loads exactly those, most
# cited first, at one request per second. Resumable: pages replay from data/raw/US and
# decisions already in the database are skipped.
load-us: venv ## Load the ÚS decisions the corpus cites, e.g. make load-us LIMIT=40
	$(PY) scripts/load_us_citations.py $(if $(LIMIT),--limit $(LIMIT),)

extract: venv ## Extract citations from the crawled corpus
	$(JG) extract

classify: venv ## Classify citation treatments
	$(JG) classify

# --- evaluation ------------------------------------------------------------

eval: venv ## Full evaluation table (extraction + treatment)
	$(PY) eval/report.py

eval-extract: venv ## Extraction recall and precision only
	$(PY) eval/report.py --extraction-only

# --- deployment ------------------------------------------------------------
# Azure free account, Terraform in infra/azure. See infra/azure/README.md.
# Nothing here builds a container image or a CI pipeline: CLAUDE.md rules both out, and a
# jar under systemd needs neither.

TF      := terraform -chdir=infra/azure
TF_OUT   = $(TF) output -raw
REMOTE   = azureuser@$$($(TF_OUT) fqdn)
APP_JAR := api/target/judikat-guard-api-0.1.0-SNAPSHOT.jar
APP_DIR := /opt/judikat-guard

tf-init: ## terraform init for the Azure stack
	$(TF) init

tf-plan: ## Show what the Azure stack would change
	$(TF) plan

tf-apply: ## Create or update the Azure stack
	$(TF) apply

tf-output: ## Show the stack's outputs (URL, host, ssh command)
	$(TF) output

tf-destroy: ## Delete every Azure resource in the stack
	$(TF) destroy

# The payload is the jar PLUS extract/patterns.toml and prompts/, which are deliberately not
# packaged inside the fat jar: PatternSet and PromptTemplate read them from disk, walking up
# from the working directory, so that a stale copy baked into a build can never diverge from
# the file the pipeline uses. Ship them or the API starts and then fails on first request.
deploy: web-install ## Build and ship the app to the Azure VM
	$(MVN) -q -f api/pom.xml package -DskipTests
	cd web && npm run build
	@test -f $(APP_JAR) || { echo "missing $(APP_JAR)"; exit 1; }
	ssh $(REMOTE) 'sudo systemctl stop judikat-guard || true'
	scp $(APP_JAR) $(REMOTE):$(APP_DIR)/app.jar
	scp extract/patterns.toml $(REMOTE):$(APP_DIR)/extract/patterns.toml
	scp prompts/*.md $(REMOTE):$(APP_DIR)/prompts/
	rsync -a --delete web/dist/ $(REMOTE):$(APP_DIR)/web/
	@$(MAKE) --no-print-directory deploy-env
	ssh $(REMOTE) 'sudo chown -R azureuser:judikat $(APP_DIR) && sudo systemctl start judikat-guard'
	@echo "deployed: $$($(TF_OUT) url)"

# Secrets travel on stdin, never on the command line: an argument would be visible in `ps`
# on the remote host for the life of the command.
deploy-env: ## Write the remote EnvironmentFile from terraform outputs + GEMINI_API_KEY
	@test -n "$$GEMINI_API_KEY" || echo "warning: GEMINI_API_KEY unset — the proposition check will answer 503"
	@printf 'JG_JDBC_URL=%s\nJG_DB_USER=%s\nJG_DB_PASSWORD=%s\nGEMINI_API_KEY=%s\n' \
	  "$$($(TF_OUT) jdbc_url)" "$$($(TF_OUT) postgres_user)" \
	  "$$($(TF_OUT) postgres_password)" "$$GEMINI_API_KEY" \
	  | ssh $(REMOTE) 'sudo tee $(APP_DIR)/env >/dev/null \
	      && sudo chown judikat:judikat $(APP_DIR)/env && sudo chmod 0640 $(APP_DIR)/env'

# Flyway builds the schema on first API boot, so this carries data only. Re-runnable: the
# --clean drops what a previous run left behind.
seed-remote: ## Restore the local corpus into the Azure database (~124 MB over your uplink)
	pg_dump -Fc -Z9 --no-owner --no-acl "$(JG_DB_URL)" \
	  | pg_restore --no-owner --no-acl --clean --if-exists --exit-on-error \
	      -d "$$($(TF_OUT) psql_url)"

deploy-logs: ## Tail the API's log on the VM
	ssh $(REMOTE) 'sudo journalctl -u judikat-guard -f -n 100'

deploy-smoke: ## Check the deployed API answers
	@curl -fsS "$$($(TF_OUT) url)/api/corpus" && echo && echo "ok"

# The application has no authentication and CLAUDE.md forbids adding any. This puts a gate
# in front of it at the proxy instead, which is infrastructure rather than an app feature.
# See "Exposure" in infra/azure/README.md before deciding whether you want it.
protect: ## Password-gate the model-calling endpoints, e.g. make protect USER=demo PASS=...
	@test -n "$(USER)" -a -n "$(PASS)" || { echo "usage: make protect USER=demo PASS=secret"; exit 2; }
	@ssh $(REMOTE) "HASH=\$$(caddy hash-password --plaintext '$(PASS)') && \
	  printf '@model path /api/decisions/*/proposition-check /api/citations/*/proposition-check\nbasic_auth @model {\n\t$(USER) %s\n}\n' \"\$$HASH\" \
	    | sudo tee /etc/caddy/conf.d/10-protect.conf >/dev/null && sudo systemctl reload caddy"
	@echo "model endpoints now require $(USER)'s password"

unprotect: ## Remove the password gate
	ssh $(REMOTE) 'sudo rm -f /etc/caddy/conf.d/10-protect.conf && sudo systemctl reload caddy'

# --- tests -----------------------------------------------------------------

test: test-java test-python ## Run every test

test-java: ## mvn test
	$(MVN) -f api/pom.xml test

test-python: venv ## pytest
	$(PYTEST) pipeline -q

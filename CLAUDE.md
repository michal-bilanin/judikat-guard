# CLAUDE.md

Operating instructions for this repository. Read `PLAN.md` before starting any task; it holds the domain glossary, schema, milestones, and design rationale.

## What this project is

An automated validity checker for Czech legal sources. Given a legal document, it reports every court decision and statutory provision the document relies on, each with a traffic light and traceable evidence. Hackathon proof of concept, three-day budget.

## Runtimes

| Directory | Runtime | Owns |
|---|---|---|
| `api/` | Java 25 (LTS), Spring Boot 4.1, Maven | Schema migrations, HTTP API, rules engine, query-time model calls |
| `pipeline/` | Python 3.12 | Crawling, normalisation, citation extraction, batch classification |
| `web/` | Vite, React, TypeScript | One page: upload, traffic lights, evidence panel |
| `prompts/` | Markdown | Versioned prompt templates read by both api and pipeline |

## Commands

```
make up            docker-compose postgres + pgvector
make migrate       flyway via the api module
make api           spring boot run
make web           vite dev server
make crawl COURT=NSS
make extract
make classify
make eval
make test          mvn test + pytest

make tf-apply      provision the Azure stack (infra/azure)
make deploy        build and ship jar + prompts + patterns + web to the VM
make seed-remote   restore the local corpus into the cloud database
```

Deployment lives in `infra/azure/` (Terraform + cloud-init). Read `infra/azure/README.md`
before applying: the public IP is the one component that always bills, and the app reaches
the public internet with no authentication and a model-calling endpoint behind it.

Add a Makefile target for any command you run more than twice. Never document a bare command in a commit message when a target would do.

## Hard rules

1. **Never invent an identifier.** Case numbers, ECLIs, journal numbers, act numbers, section numbers. If a fixture or test needs one, take it from crawled data or mark it clearly with a `TEST-` prefix that cannot resolve.
2. **Never produce a verdict without evidence.** Every `Light` other than GREEN must be backed by a `treatment` or `provision_version` row. If the rules engine cannot cite a row, it returns GREEN with the "no adverse treatment found" phrasing.
3. **Never store an unvalidated model label.** Every classification response must carry an `evidence_span` that is a literal substring of the supplied context after whitespace normalisation. Retry once, then write `UNCLASSIFIED`. Do not relax this to make a test pass.
4. **Flyway owns the schema.** All DDL lives in `api/src/main/resources/db/migration`. The Python pipeline reads and writes rows only. Never add Alembic or issue `CREATE TABLE` from Python.
5. **Cache every HTTP fetch to `data/raw/`.** Re-running a crawl must issue zero network requests for already-fetched pages. Rate limit to one request per second per host and honour robots.txt.
6. **Only crawl the allowlisted hosts:** `nalus.usoud.cz`, `nssoud.cz`, `vyhledavac.nssoud.cz`, `rozhodnuti.nsoud.cz`, `opendata.eselpoint.cz`, `e-sbirka.gov.cz`. Stop and ask before adding any other source. Two were added on explicit authorisation: `vyhledavac.nssoud.cz` on 2026-09-02, because `www.nssoud.cz` publishes no decision text and NSS was otherwise uncrawlable; `e-sbirka.gov.cz` on 2026-09-03, because `opendata.eselpoint.cz` was retired and now answers every path with a redirect notice. The list here and `ALLOWLISTED_HOSTS` in `pipeline/jg/config.py` must stay identical.
7. **Model calls are temperature 0, JSON-schema constrained, and cached** on the key given in PLAN.md section 8. Every persisted row records `model` and `prompt_version`.
8. **Prompts change by adding a new versioned file**, never by editing an existing one in place. `treatment-classify.v1.md` stays frozen once rows reference it.
9. **Keep the verdict layer pure.** `StatusEngine` takes evidence records and a date, returns a `Status`. No repository calls, no clock reads, no I/O. It is the most tested class in the repo.

## Do not add

Neo4j, Kafka, Elasticsearch, Redis, JPA/Hibernate, a second service, authentication, user accounts, multi-tenancy, Docker builds for the app itself, CI pipelines, or an ORM layer over the graph queries. Postgres plus hand-written SQL is the whole data platform at this scale. If you believe one of these is genuinely required, say so and wait rather than adding it.

## Style

- Java 25: records for DTOs, sealed interfaces for closed variant sets, `JdbcClient` for queries, constructor injection, no field injection, no Lombok. Use `Gatherers.windowSliding` for paragraph context windows rather than index arithmetic. Use `ScopedValue` for request-scoped evaluation context, but never inside `StatusEngine`. Annotate every package `@NullMarked` (JSpecify) and treat nullability warnings as errors.
- Spring Boot 4: the web starter is `spring-boot-starter-webmvc`. Test slice annotations live under `org.springframework.boot.<module>.test.autoconfigure`. Enable virtual threads via `spring.threads.virtual.enabled=true`. Do not copy configuration from Spring Boot 3 examples; deprecated APIs were removed in 4.0.
- **No preview features.** Structured concurrency and primitive patterns are still preview in Java 25. Do not add `--enable-preview` to the build. If you believe a preview feature is needed, stop and ask.
- Python: typer for CLI, pydantic for parsed shapes, no notebooks, no global state, every regex pattern gets a unit test with a real citation string from crawled data.
- SQL: explicit column lists, no `select *` outside exploratory scripts.
- Tests: the rules engine gets a parameterised truth table covering every row of the evaluation table in PLAN.md section 9, plus precedence cases.

## Language and terminology

The UI and all user-facing text is Czech. Use Czech legal vocabulary: *překonáno*, *argumentačně oslabeno*, *zrušeno*, *zúženo*. Never use translated common-law terms like "overruled" or "distinguished" in user-facing output, because Czech law has no doctrine of binding precedent and the mismatch will read as incompetence to a legal audience. Internal code identifiers stay in English and use the label enum from PLAN.md section 8.

## When you are unsure

Ask rather than guess on: which court to crawl next, whether a treatment label fits, whether a provision change is material, and anything touching how a verdict is worded. Guess freely on: naming, file layout inside a module, test structure, and CSS.

## Definition of done for any task

The relevant `make` target runs clean, tests pass, and the change is reflected in `PLAN.md` if it altered a design decision, schema, or milestone boundary.

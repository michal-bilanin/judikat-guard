# Judikát Guard: implementation plan

An automated validity checker for Czech legal sources. Paste a legal document, get back every court decision and statutory provision it relies on, each with a traffic light and the evidence behind that verdict.

Built as a mini-hackathon proof of concept. Target: three days solo.

---

## 1. Domain glossary

Written for a non-lawyer implementer. These terms appear throughout the schema and code.

| Term | Meaning |
|---|---|
| **judikatura** | The body of published court decisions. Lawyers cite them to support arguments. |
| **spisová značka / č. j.** | Case reference numbers. Two different formats, both in circulation, both used in citations. |
| **ECLI** | European Case Law Identifier. A stable machine-readable ID that Czech courts publish. Use it as the primary key. |
| **R-číslo** | A journal number assigned when a decision is published in the official Supreme Court reporter. Its presence signals higher authority. |
| **právní věta** | A short headnote stating the legal rule the decision establishes. Not always present. |
| **senát** | A panel of judges. The standard unit that decides a case. |
| **rozšířený senát** (NSS) | Extended panel. A normal NSS panel that wants to depart from existing NSS case law must refer the question here. Its decisions are therefore near-certain interpretation changes. |
| **velký senát** (NS) | Grand panel. Same mechanism at the Supreme Court. |
| **plénum** (ÚS) | Full assembly of the Constitutional Court. Same mechanism there. |
| **výrok** | The operative part of a decision, the actual order. If it annuls another decision, it names it explicitly. |
| **derogace** | The Constitutional Court striking down a statutory provision entirely. Anything relying on that provision is undermined. |
| **přechodná ustanovení** | Transitional provisions in an amending act, stating which version of a rule applies to which situations. |

Three courts matter:

- **ÚS** (Ústavní soud, Constitutional Court). Can annul decisions of the other two by name, and can strike down legislation.
- **NSS** (Nejvyšší správní soud, Supreme Administrative Court). Administrative law.
- **NS** (Nejvyšší soud, Supreme Court). Civil and criminal law.

---

## 2. What the system asserts

The output is never "this is valid". It is always scoped:

> For source X as used in your document, evaluated as of date D, against a corpus of N decisions published through date C: no adverse treatment found / narrowed in scope by Y / departed from by Z / the provision it relies on was reworded on date T.

Every verdict carries a list of evidence rows. Every evidence row points at a real paragraph in a real document. Nothing is asserted without a traceable source.

---

## 3. Core design decisions

Read these before writing any code. They are the difference between a defensible tool and a demo that falls apart under questioning.

**D1. Structural signals before language models.** Most overruling events in Czech law are procedurally marked: the panel type of the citing decision, or an annulment named in the výrok. These are metadata lookups and regex matches. Use the model only for the genuinely ambiguous middle. This is cheaper and vastly easier to explain.

**D2. Separate the evidence layer from the verdict layer.** The model produces labelled relationships and nothing else. A deterministic rules engine in Java turns those labels into traffic lights. Consequence: the verdict policy can change without re-running any inference, and the UI can always show exactly which facts produced a red light.

**D3. Model output must carry a verbatim evidence span.** Every classification returns a quote from the supplied context. Reject any response whose quote is not literally present in the input. This is the cheapest hallucination gate available and it eliminates most fabricated labels.

**D4. Verdicts computed on read, not materialised.** A status evaluation is a handful of indexed queries plus a pure function. Computing on demand avoids a whole class of staleness bugs. Add caching only if measured latency demands it.

**D5. Legal authority relations live in a table, not in code.** Which body may depart from which court's case law is seeded data (`departure_authority`). Makes the legal rules inspectable and adjustable without recompiling.

**D6. Flyway owns the schema.** The Spring module holds all migrations. The Python pipeline reads and writes rows but never issues DDL. One source of truth for the schema.

**D7. Prompts are versioned files at the repo root, shared by both runtimes.** `prompts/` is read by Python (batch) and Java (query time). Every stored classification records which prompt version produced it.

**D8. Asymmetric thresholds.** A false red destroys trust permanently. A false amber costs the user thirty seconds. Set a high bar for red and accept lower recall there. Document the tradeoff, it is a product-judgment signal.

---

## 4. Stack

| Layer | Choice | Why |
|---|---|---|
| Ingestion, extraction, batch classification | Python 3.12, httpx, selectolax, pydantic, typer | Scraping and text munging are fastest here |
| Schema, API, rules engine | Java 25 (LTS), Spring Boot 4.1, Maven 3.9 | The rules engine is the crown jewel and belongs in a typed, testable language |
| DB access | Flyway plus `JdbcClient` | Hand-written SQL. The graph queries and recursive CTEs are the point; JPA gets in the way |
| Database | PostgreSQL 16 with pgvector | Recursive CTEs cover one-hop propagation. Vectors for ratio retrieval and near-duplicate detection |
| Frontend | Vite, React, TypeScript, TanStack Query | One page. Do not gold-plate |
| Model calls | Plain `RestClient` behind an `LlmClient` interface (Java), `httpx` behind the same contract (Python) | Avoid novel dependencies in a hackathon. Spring AI is an optional swap later |
| Tests | JUnit 5, Testcontainers (Postgres), pytest | The rules engine gets a parameterised truth table |

No Neo4j. No Kafka. No Elasticsearch. No service mesh. At this corpus size Postgres is the whole data platform.

### Java 25 specifics

Java 25 is the current LTS, released September 2025. Spring Boot 4.1 is the matching GA line, built on Spring Framework 7, with first-class Java 25 support and a Java 17 baseline. Set `<maven.compiler.release>25</maven.compiler.release>` and use `eclipse-temurin:25-jdk` if you containerise.

Four language and runtime features this project should actually use:

**Virtual threads.** Set `spring.threads.virtual.enabled=true`. The document-check endpoint is almost pure I/O: many small indexed reads plus a few model calls. Platform threads buy nothing here.

**Scoped values (JEP 506, final in 25).** Carry the evaluation context (`asOf`, corpus coverage dates, active prompt version) through the read path without threading a context parameter through every repository signature. Bind once in a servlet filter, read in the report assembly layer. Explicitly *not* inside `StatusEngine`: that stays a pure function with explicit parameters so the truth table test can call it directly. Scoped values are for the plumbing around the engine, never inside it.

**Stream gatherers (JEP 485, final in 24).** `Gatherers.windowSliding` is the natural fit for the paragraph context window (a citation's paragraph plus two either side) and `windowFixed` for batching citations into classification chunks. Replaces the hand-rolled index arithmetic that this kind of code otherwise accumulates.

**JSpecify null safety.** Spring Boot 4 annotates its own API with JSpecify. Add `@NullMarked` at each package and let the build fail on nullability violations. Cheap correctness win in a schema where a missing `journal_no`, an absent `právní věta`, and an unresolved `cited_ecli` are all normal states rather than errors.

**Deliberately not used:** structured concurrency is still a preview feature in Java 25 and requires `--enable-preview` on compile, test, and run, which complicates the Maven config and the Testcontainers setup for no real gain. Virtual threads plus a plain executor cover the fan-out here. Do not enable preview flags on this project.

Spring Boot 4 migration facts that will cost an hour each if discovered late:

- The starter is `spring-boot-starter-webmvc`, not `spring-boot-starter-web`. Boot 4 split the codebase into smaller modules.
- Test slice annotations moved packages, for example `org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest`.
- Jakarta EE 11 baseline, Tomcat 11 embedded.
- Everything deprecated in Spring Boot 3 has been removed. Do not copy config snippets from 3.x tutorials.
- Observability modules were renamed (`spring-boot-micrometer-metrics` and siblings). Irrelevant here since we add no observability, but it explains why old examples fail to resolve.

---

## 5. Repository layout

```
/
  CLAUDE.md                     agent operating instructions
  PLAN.md                       this file
  docker-compose.yml            postgres + pgvector
  Makefile                      one-line entry points for every stage
  prompts/
    treatment-classify.v1.md
    proposition-check.v1.md
    provision-materiality.v1.md
  api/                          Spring Boot, Maven
    pom.xml
    src/main/java/tech/judikatguard/
      JudikatGuardApplication.java
      status/                   StatusEngine, EvidenceRow, Light, Reason
      decision/                 read repositories + DTOs
      document/                 upload + check orchestration
      llm/                      LlmClient, PropositionChecker
      web/                      controllers
    src/main/resources/
      application.yml
      db/migration/             V1__init.sql, V2__authority_seed.sql, ...
    src/test/java/...
  pipeline/                     Python
    pyproject.toml
    jg/
      cli.py                    typer entry point
      crawl/                    nalus.py, nssoud.py, nsoud.py, esbirka.py
      normalize.py
      extract/                  patterns.py, resolver.py, anaphora.py
      classify/                 router.py, structural.py, reasoning.py
      db.py
  web/                          Vite + React + TS
  eval/
    labels.csv                  hand-labelled ground truth
    report.py                   prints precision/recall
  data/                         gitignored: raw HTML cache, e-Sbírka dumps
```

---

## 6. Schema

`api/src/main/resources/db/migration/V1__init.sql`:

```sql
create extension if not exists vector;

create table court (
  code text primary key,
  name text not null
);

create table decision (
  ecli            text primary key,
  court_code      text not null references court(code),
  panel_type      text not null,          -- panel | extended | grand | plenary | unknown
  decided_on      date not null,
  case_no         text,                   -- spisová značka
  ref_no          text,                   -- č. j.
  journal_no      text,                   -- R-číslo, null if unpublished
  ratio_summary   text,
  ratio_embedding vector(1536),
  source_url      text not null,
  fetched_at      timestamptz not null
);
create index on decision (court_code, panel_type);
create index on decision (decided_on);

create table decision_alias (
  alias      text primary key,
  alias_kind text not null,               -- case_no | ref_no | journal_no | ecli_variant
  ecli       text not null references decision(ecli)
);

create table decision_paragraph (
  ecli text not null references decision(ecli),
  idx  int  not null,
  body text not null,
  primary key (ecli, idx)
);

create table provision (
  id      bigserial primary key,
  act_no  text not null,                  -- '89/2012'
  section text not null,                  -- '2000'
  subsec  text,
  unique (act_no, section, subsec)
);

create table provision_version (
  id           bigserial primary key,
  provision_id bigint not null references provision(id),
  body         text not null,
  valid_from   date not null,
  valid_to     date,
  derogated_by text references decision(ecli)
);
create index on provision_version (provision_id, valid_from);

create table citation (
  id              bigserial primary key,
  citing_ecli     text not null references decision(ecli),
  cited_ecli      text references decision(ecli),
  cited_provision bigint references provision(id),
  paragraph_idx   int,
  raw_text        text not null,
  extractor       text not null,          -- rules | llm
  constraint citation_target check (cited_ecli is not null or cited_provision is not null)
);
create index on citation (cited_ecli);
create index on citation (cited_provision);

create table treatment (
  id             bigserial primary key,
  citation_id    bigint not null references citation(id),
  label          text not null,           -- see section 8
  confidence     numeric(3,2) not null,
  evidence_span  text not null,
  route          text not null,           -- structural | triage | reasoning
  model          text,
  prompt_version text not null,
  created_at     timestamptz not null default now(),
  unique (citation_id, prompt_version)
);
create index on treatment (citation_id);

create table corpus_meta (
  court_code    text primary key references court(code),
  decision_count int not null,
  covered_through date not null,
  refreshed_at  timestamptz not null
);
```

`V2__authority_seed.sql` seeds `departure_authority`, encoding who may override whom:

```sql
create table departure_authority (
  citing_court text not null,
  citing_panel text not null,
  cited_court  text not null,
  binding      boolean not null,
  note         text,
  primary key (citing_court, citing_panel, cited_court)
);

insert into departure_authority values
  ('NSS','extended','NSS', true,  'rozšířený senát, §17 s.ř.s.'),
  ('NS', 'grand',   'NS',  true,  'velký senát, §20 ZSS'),
  ('US', 'plenary', 'US',  true,  'plénum, §23 z. o ÚS'),
  ('US', 'panel',   'NSS', true,  'Art. 89(2) Ústavy'),
  ('US', 'panel',   'NS',  true,  'Art. 89(2) Ústavy'),
  ('US', 'plenary', 'NSS', true,  null),
  ('US', 'plenary', 'NS',  true,  null),
  ('NSS','panel',   'NSS', false, 'panel cannot depart, must refer'),
  ('NS', 'panel',   'NS',  false, 'panel cannot depart, must refer');
```

A `DEPARTED` label from a `(court, panel)` pair whose `binding` is false becomes an amber conflict, not a red supersession. That distinction is the legally correct behaviour and it falls out of a join.

---

## 7. Extraction

The ceiling on everything downstream. Budget real time here.

**Rules pass** (`pipeline/jg/extract/patterns.py`). Named patterns, each with a unit test:

```python
REF_NO      = r"č\.\s*j\.\s*(\d+\s+[A-Z][a-zA-Z]{1,4}\s+\d+/\d{4})(?:\s*-\s*(\d+))?"
CASE_NO_US  = r"sp\.\s*zn\.\s*((?:Pl\.|[IVX]+\.?)\s*ÚS\s+\d+/\d{2,4})"
CASE_NO_GEN = r"sp\.\s*zn\.\s*(\d+\s+[A-Z][a-zA-Z]{1,4}\s+\d+/\d{4})"
ECLI        = r"ECLI:CZ:(US|NSS|NS):\d{4}:[\w.]+"
JOURNAL     = r"\bR\s+(\d+)/(\d{4})\b"
PROVISION   = r"§\s*(\d+[a-z]?)(?:\s*odst\.\s*(\d+))?(?:[^.]{0,40}?zák(?:ona|\.)\s*č\.\s*(\d+/\d{4})\s*Sb\.)?"
```

**Resolution** (`resolver.py`). Every extracted reference resolves against `decision_alias` to an ECLI. Unresolved references are persisted with `cited_ecli = null` and counted; they are a coverage metric, not a silent drop.

**Provision anaphora.** `PROVISION` frequently omits the act ("§ 2000 odst. 1" with no act number). Resolve by scanning backwards in the same document for the most recent explicit act number, falling back to the act most cited in that document. Log the resolution basis.

**Model pass** (`anaphora.py`). Runs only on sentences that contain a citation trigger word but produced no rule match. Its job is expressions like *citovaného rozhodnutí*, *tamtéž*, *výše uvedeného nálezu*, *obč. zák.* Context window: the sentence plus the preceding three paragraphs. Output must name a candidate already extracted from that document; anything else is discarded.

**Cross-runtime parity.** Extraction runs in two places: Python over the crawled corpus, Java over documents uploaded at query time. Do not write it twice from scratch. Keep the patterns in `extract/patterns.toml` at the repo root, load them from both runtimes, and add a golden fixture (`eval/extraction-golden.jsonl`) that both implementations must reproduce identically. The parity test fails the build on divergence. The patterns above are deliberately restricted to syntax that behaves the same in `java.util.regex` and Python `re`: no lookbehind, no possessive quantifiers, no named-group syntax differences. Exposing the Python extractor over HTTP instead is acceptable, but it adds a second process to the demo.

**Acceptance:** on a 30-document hand-checked sample, rules-only recall ≥ 0.80 and precision ≥ 0.95. Report both numbers, they cap the whole system.

---

## 8. Treatment labels

Seven labels. Do not add an eighth without editing this plan.

| Label | Meaning |
|---|---|
| `FOLLOWED` | Applies the cited rule and agrees with it |
| `MENTIONED` | Background reference, no reliance |
| `DISTINGUISHED` | Accepts the rule, holds the facts differ |
| `NARROWED` | Accepts the rule but limits where it applies |
| `DEPARTED` | Replaces the cited interpretation |
| `CRITICIZED` | Disagrees without authority to change anything |
| `QUASHED` | Formally annuls the cited decision |
| `UNCLASSIFIED` | Model failed validation twice. Surfaced as amber "needs review" |

### Routing (`classify/router.py`)

Three tiers. Cost control lives here.

1. **Structural, no model call.**
   - Citation appears inside a party-submission passage (markers: *stěžovatel odkazuje*, *žalobce namítá*, *podle stěžovatele*, *dovolatel poukazuje*) → `MENTIONED`, confidence 0.95, route `structural`.
   - Citing decision's výrok annuls the cited decision by reference number → `QUASHED`, confidence 1.0.
2. **Forced escalation to the reasoning model.**
   - Citing decision `panel_type` in (`extended`, `grand`, `plenary`).
   - Or surrounding text matches a departure marker: *odchýlit se*, *nelze setrvat*, *překonán*, *koriguje*, *mění dosavadní*, *nesouhlasí s právním názorem*, *postoupil rozšířenému senátu*, *postoupil velkému senátu*.
3. **Triage then escalate.** Everything else goes to a cheap classifier. Escalate anything below confidence 0.75.

On a 3000-decision corpus this should reduce roughly 20k edges to about 1500 reasoning calls, batchable overnight.

### Prompt contract

`prompts/treatment-classify.v1.md` receives: the cited decision's `ratio_summary`, the citing paragraph plus two paragraphs either side, and metadata (both courts, panel type, both dates). It returns strict JSON:

```json
{
  "label": "NARROWED",
  "confidence": 0.82,
  "evidence_span": "verbatim substring of the supplied context",
  "reasoning": "one sentence"
}
```

**Validation, non-negotiable:** normalise whitespace, then assert `evidence_span` is a substring of the supplied context. On failure, retry once with the violation stated. On second failure, write `UNCLASSIFIED`. Never store an unvalidated label.

Temperature 0. Cache responses keyed on `(citing_ecli, cited_ecli, paragraph_idx, prompt_version)`.

---

## 9. Rules engine (Java, the crown jewel)

`api/src/main/java/tech/judikatguard/status/StatusEngine.java`. A pure function, no I/O, fully unit tested.

```java
public record EvidenceRow(
    long citationId, String citingEcli, String citingCourt, String citingPanel,
    LocalDate citingDate, TreatmentLabel label, BigDecimal confidence,
    String evidenceSpan, Integer paragraphIdx, boolean citingMayDepart) {}

public sealed interface Reason {
    record Quashed(String byEcli, String span) implements Reason {}
    record Superseded(String byEcli, String panel, String span) implements Reason {}
    record Conflict(String byEcli, String span) implements Reason {}
    record Narrowed(String byEcli, String span) implements Reason {}
    record ProvisionReworded(long provisionId, LocalDate changedOn, boolean material) implements Reason {}
    record ProvisionDerogated(long provisionId, String byEcli) implements Reason {}
    record InheritedWeakness(String viaEcli) implements Reason {}
}

public enum Light { GREEN, AMBER, RED }

public record Status(String ecli, Light light, LocalDate asOf,
                    int corpusSize, LocalDate corpusThrough, List<Reason> reasons) {}
```

Evaluation order, first match wins on RED:

| Condition | Light | Reason |
|---|---|---|
| Any `QUASHED` | RED | `Quashed` |
| Any `DEPARTED` where `citingMayDepart` | RED | `Superseded` |
| Relied-on provision has `derogated_by` | RED | `ProvisionDerogated` |
| Any `DEPARTED` where not `citingMayDepart` | AMBER | `Conflict` |
| Any `NARROWED` | AMBER | `Narrowed` |
| Relied-on provision reworded after `decided_on` and judged material | AMBER | `ProvisionReworded` |
| Any `UNCLASSIFIED` | AMBER | needs review |
| One-hop: a source this decision wholly relies on is RED | AMBER | `InheritedWeakness` |
| Otherwise | GREEN | empty, rendered as "no adverse treatment found" |

Exactly one hop of propagation. Deeper propagation is unexplainable and noisy.

**Test:** a JUnit `@ParameterizedTest` fed a CSV of evidence combinations and expected lights, including every row above plus precedence cases (quashed beats narrowed, red beats inherited amber).

---

## 10. The provision-rewording check

The differentiator. A decision from 2013 interpreting a rule reworded in 2017 can have a spotless citation history and be worthless. No keyword system catches this, because no other document ever criticised it.

1. Ingest e-Sbírka open data from `opendata.eselpoint.cz` (bulk JSON/JSON-LD dumps; the REST API needs registration by datová schránka, too slow for a hackathon) into `provision` and `provision_version`.
2. For each provision a decision relies on, find the version in force at `decided_on` and the version in force at `asOf`.
3. If the text differs, one model call against `prompts/provision-materiality.v1.md` decides whether the change touches the reasoning or is cosmetic (renumbering, terminology alignment). Returns `{ "material": bool, "confidence": float, "evidence_span": "..." }`.
4. Material → amber, with both wordings shown side by side in the UI.

Cache aggressively: keyed on `(provision_version_from, provision_version_to)`, independent of which decision triggered it. Many decisions share provisions, so the cache hit rate is high.

---

## 11. API

Spring MVC (`spring-boot-starter-webmvc`), records as DTOs, virtual threads enabled, springdoc for the schema.

```
GET  /api/health
GET  /api/decisions/{ecli}                    metadata + ratio summary
GET  /api/decisions/{ecli}/status?asOf=       traffic light + reasons
POST /api/documents/check                     body: { text } -> DocumentReport
POST /api/citations/{id}/proposition-check    body: { claim } -> PropositionVerdict
GET  /api/corpus                              size + coverage dates per court
```

`DocumentReport`:

```json
{
  "asOf": "2026-09-01",
  "corpus": { "NSS": { "count": 3142, "through": "2026-08-15" } },
  "sources": [
    {
      "ecli": "ECLI:CZ:NSS:2015:6.Ads.45.2014.32",
      "rawText": "rozsudek NSS ze dne 12. 3. 2015, č. j. 6 Ads 45/2014-32",
      "light": "AMBER",
      "reasons": [ { "kind": "NARROWED", "byEcli": "...", "span": "...", "paragraph": 34 } ]
    }
  ],
  "unresolved": [ { "rawText": "...", "reason": "not in corpus" } ]
}
```

`unresolved` is a first-class field, not an error. Being explicit about coverage gaps is the product.

Document check runs extraction on the uploaded text and then reads the existing graph. The only new work is extraction; no batch classification at request time.

---

## 12. Proposition check

The feature most likely to win the room. Not "is this source current" but "are you using it correctly".

Input: a citation from the user's document plus the claim it is offered to support. The model compares that claim against what the decision actually held and returns:

```json
{ "verdict": "SUPPORTS|OVERBROAD|UNRELATED|CONTRADICTS",
  "confidence": 0.0, "evidence_span": "", "note": "" }
```

The demo line: *"You cite this decision for X, but it held X only for movable property."* Runs in Java via `LlmClient`, reading `prompts/proposition-check.v1.md`.

---

## 13. Frontend

One page. Textarea or file drop, then a list of sources, each row a traffic light plus the raw citation as it appeared. Click a row to expand the evidence: the label, the citing decision, the verbatim span, a link to the source paragraph. A second panel lists unresolved references with the coverage caveat.

The evidence panel is the entire product. Spend one evening maximum on everything else.

---

## 14. Milestones

Vertical slice first, then deepen. Each milestone ends in a runnable, checkable state.

**M0 Scaffold.** docker-compose Postgres with pgvector, Flyway V1 and V2 applying cleanly, Spring Boot answering `/api/health`, Python CLI with `jg --help`, Makefile targets. *Done when* `make up && make migrate && curl localhost:8080/api/health` returns 200.

**M1 Corpus, NSS only.** Crawler with on-disk raw HTML cache and polite rate limiting. Normalise into `decision`, `decision_alias`, `decision_paragraph`. Populate `corpus_meta`. Start with one court: NSS has the cleanest structure and the referral mechanism that gives you free signal. *Done when* 1000+ NSS decisions are loaded with panel_type correctly detected and a re-run makes zero HTTP requests.

**M2 Extraction.** Rules pass plus resolution, recall and precision measured against a 30-document hand-checked sample. *Done when* the numbers are in `eval/` and printed by `make eval-extract`.

**M3 Structural classification only.** No model calls. Party-submission `MENTIONED`, výrok-based `QUASHED`, plus `DEPARTED` candidates flagged by panel type. *Done when* `treatment` has rows and at least one genuine red light exists in the data.

**M4 Rules engine, API, UI. ← cut line.** Everything above wired end to end. A working demo exists from here on. *Done when* pasting a real document into the web page returns lights with expandable evidence.

**M5 Reasoning classification.** Router tiers 2 and 3, prompt contract, evidence-span validation, response cache, batch runner. *Done when* the fuzzy middle is labelled and `UNCLASSIFIED` rate is under 5%.

**M6 Provision layer.** e-Sbírka ingest, version resolution, materiality call. *Done when* a decision with a clean citation history correctly shows amber because its provision was reworded. This is the screenshot for the presentation.

**M7 Proposition check.** *Done when* an overbroad citation is flagged in the demo document.

**M8 Eval report.** 50 hand-labelled relationships in `eval/labels.csv`, per-label precision and recall, extraction recall, `make eval` prints the table. *Done when* the numbers are on a slide.

Suggested three-day split: M0 to M4 on day one, M5 and M6 on day two, M7 and M8 plus rehearsal on day three. If day three runs short, cut M7 before M8. The numbers matter more than the extra feature.

---

## 15. Evaluation

`eval/labels.csv`: `citing_ecli, cited_ecli, paragraph_idx, gold_label, note`. Fifty rows, spread across all seven labels, hand-labelled by you with the decision text open.

`eval/report.py` prints:

- Per-label precision and recall for treatment classification.
- Extraction recall and precision from the M2 sample.
- Confusion matrix, with the `DISTINGUISHED` / `NARROWED` / `DEPARTED` cluster highlighted, because that is where the reasoning model earns its keep.
- Red-light precision as its own headline number.

Tune thresholds so red-light precision is high even at the cost of recall, per D8, and say so in the presentation.

---

## 16. Stated limitations

Put these on a slide yourself. Judges trust people who list their own failure modes.

- Extraction recall caps everything. Quote the measured number.
- Not every decision is published, so the corpus has holes. This is why the UI says "no adverse treatment found in N decisions through date C" and never "valid".
- Language models over-detect conflict. Mitigated by the mandatory evidence span and the high red threshold.
- One-hop propagation only. Deeper weakening chains exist and are not modelled.
- Materiality judgements on reworded provisions are model opinions, always shown with both wordings so the user can check.
- Czech law has no doctrine of binding precedent. The UI must say *překonáno* or *argumentačně oslabeno*, never a translated common-law term. Getting this vocabulary wrong will cost credibility with a legal audience within the first minute.

---

## 17. Demo script

Four minutes, in this order:

1. Paste a real brief. Lights appear. Expand one amber to show the evidence span and paragraph link. Establishes that it works and that it explains itself.
2. Show the provision-rewording amber. State plainly that no keyword system can find this, because nothing was ever said against the decision. This is the intellectual centre of the pitch.
3. Show the proposition check catching an overbroad citation. Reframes the tool from "checker" to "reviewer".
4. Show the eval table and the red-light precision number. Then read the limitations slide aloud.

Close on the architecture point: the model labels relationships, a deterministic engine assigns verdicts, and every verdict traces to a paragraph. That is what makes it deployable in a domain where being wrong is expensive.

---

## 18. Implementation notes

Only the places where the built system diverges from or extends the plan above. Everything
not listed here was built as written.

### Schema

- **Two migrations beyond the V1/V2 that section 6 names.** `V3__court_seed.sql` inserts
  the three `court` rows: every `decision.court_code` and `corpus_meta.court_code`
  foreign key needs them, and V2 already assumes the same three codes in
  `departure_authority`. `V4__model_cache.sql` adds `llm_cache`, because sections 8 and 10
  both require a response cache and neither can be honoured without a table, plus
  `provision_materiality`, which stores the section 10 materiality verdict keyed on the
  version pair and carries its own `evidence_span` for the same reason `treatment` does.

### Rules engine

- **`Reason` gained a `NeedsReview(byEcli, citationId)` variant.** Section 9's evaluation
  table has an amber row for `UNCLASSIFIED` but lists no `Reason` to carry it, and rule 2
  forbids a non-GREEN light with nothing to cite. `NeedsReview` names the citation whose
  classification failed validation twice, so the amber still points at a row.
- **`StatusEngine.evaluate` takes an explicit `Thresholds` record** (`redMinConfidence`
  0.85, `amberMinConfidence` 0.60) rather than reading constants. This is D8 made
  testable: the truth table can drive the asymmetry directly, and the red bar can be
  raised without recompiling the engine's logic.

### Extraction

- **`extract/patterns.toml` writes some character classes out in ASCII.** Python 3 makes
  `\w` and `\d` Unicode-aware by default; `java.util.regex` does not unless
  `UNICODE_CHARACTER_CLASS` is set. Where that difference could change a match the class
  is spelled out (the ECLI suffix is `[A-Za-z0-9_.]`, not `[\w.]`). The patterns are
  otherwise exactly as section 7 gives them.
- **The toml also holds the trigger and marker word lists** (anaphora triggers,
  party-submission, departure and quashing markers) that section 8 describes in prose, for
  the same single-source-of-truth reason as the regexes.
- **`CASE_NO_US` in section 7 has been corrected, and the version printed there is the
  corrected one.** It was `(?:Pl\.\s*)?[IVX]+\.?\s*ÚS`, which makes `Pl.` an optional
  prefix to a *required* Roman numeral. Real plenary numbers carry `Pl.` *instead of* a
  numeral, never in addition to one, so the pattern matched no plenary case number at all
  in either runtime. It is now an alternation, `(?:Pl\.|[IVX]+\.?)\s*ÚS`: exactly one
  designator, either form. This was worth fixing rather than recording, because plenary
  decisions are what derogate a provision and that derogation is the `ProvisionDerogated`
  red light section 10 is built around — the gap silently removed the intellectual centre
  of the pitch from the extractable set. Verified byte-identical under `java.util.regex`
  and Python `re` across nine cases before landing, and a bare `sp. zn. ÚS 1/09` with no
  designator still matches nothing, so precision did not move. Pinned in
  `pipeline/tests/test_patterns.py`, `CitationExtractorTest` and the
  `us-caseno-plenum` / `negative-us-without-designator` entries of
  `eval/extraction-golden.jsonl`. The two plenary numbers used as fixtures
  (`Pl. ÚS 29/98`, `Pl. ÚS 36/93`) are real, read out of the NALUS page cached under
  `data/raw/US`, per rule 1.

### Provision layer

- **A derogation is dated by the annulling decision, not by the version's `valid_from`.**
  `ProvisionRepository`'s derogation lookup originally compared `provision_version
  .valid_from` against `asOf`, which answers a different question: a version in force
  from 2010 and struck down in 2027 read as derogated when asked "as of 2015". Since
  `provision_version.derogated_by` is a foreign key onto `decision(ecli)`, the annulling
  decision's `decided_on` is always available, so the lookup now joins it and filters on
  that. The old behaviour produced a false red — precisely the error D8 says destroys
  trust permanently — and the schema already carried everything needed to avoid it.
  Pinned by `derogationIsDatedByTheAnnullingDecision`, which was confirmed to fail against
  the previous query before the fix landed.
- **A provision cited directly in an uploaded document reports only derogation, never a
  rewording.** A rewording is a claim *relative to a date* — the date the reasoning that
  relied on the rule was written — and an uploaded document supplies no such date.
  Reporting one against an invented baseline would be a verdict the evidence does not
  carry, so the rewording amber appears only on the rows of decisions that rely on the
  provision, which is the framing section 10 already uses.
- **The e-Sbírka source named in section 10 step 1 no longer exists.**
  `opendata.eselpoint.cz` is now a catch-all: `/robots.txt`, `/sitemap.xml`, `/esel-esb/`,
  `/esb-otevrena-data/`, `/opendata/`, `/data/`, `/eli/`, `/api/` **and a deliberately
  nonsense path** all return HTTP 200 with one byte-identical 41,341-byte notice that the
  service moved to `gov.cz`. The nonsense path is the control that proves it is a catch-all
  rather than eight live endpoints. Every onward link (`e-sbirka.gov.cz`,
  `e-legislativa.gov.cz`, `zakony.gov.cz`) is off-allowlist, so the crawl stops there:
  adding one is a rule 6 decision for a human. Section 10's parenthetical — that the bulk
  JSON/JSON-LD dumps are the way in and the REST API needs datová schránka registration —
  is out of date and should not be trusted.
- **The replacement is better than a bulk dump: a public, date-addressable API.**
  `e-sbirka.gov.cz` was allowlisted on explicit authorisation on 2026-09-03. Its SPA config
  at `/assets/configs/env.js` names `dasexApiBasePath` = `https://e-sbirka.gov.cz/sbr-externi`,
  which is **public — no auth, no datová schránka**, and answers with structured Czech JSON
  errors such as `{"chyby":[{"kod":"NEPLATNE_STALE_URL", ...}]}`. The mechanism that makes
  section 10 work:

  ```
  GET /sbr-externi/dokumenty-sbirky/{urlencoded staleUrl}/id
      /eli/cz/sb/2012/89              -> 2057253   (current wording)
      /eli/cz/sb/2012/89/2014-01-01   -> 137687    (wording in force on that date)
  ```

  A `staleUrl` is an ELI path, it must start with `/`, and **it accepts a date**. That
  date-qualified lookup *is* the section 10 comparison — the version in force at a
  decision's `decided_on` against the version in force at `asOf` — answered by the source
  itself rather than reconstructed from a dump. Companion routes, read out of the SPA
  bundle: `/dokumenty-sbirky/{id}/detail-zneni`, `/{staleUrl}/obsah`, `/{staleUrl}/historie`,
  `/{staleUrl}/fragmenty/{fragmentId}/novelizace-a-derogace` (a *fragment* is an individual
  §, so this is `provision_version` plus `derogated_by`), and
  `/{staleUrl}/rozdilovy-obsah/{zneniCil}`, a diff between two wordings, which is the raw
  material for the materiality call. Note that `/sbr-externi/vyhledavani/...` 404s against
  an internal path of `/esel-esbir-dasex/vyhledavani/...`, so search lives on a different
  service prefix. Note also that `www.e-sbirka.gov.cz` does not resolve; only the apex does.
- **Section 10's scenario reproduced on real law, with real numbers.** Loading the version
  timelines for the four sections of the asylum act (325/1999) that the corpus actually
  cites — 134 `provision_version` rows over 26 provisions — surfaces **four rewording pairs
  that 84 distinct NSS decisions in the corpus are exposed to**, 76 of them through § 12
  alone. The changes took effect 2026-06-12 with the EU Pact on Migration and Asylum, and
  they are not cosmetic: § 12's substantive asylum test moved from the domestic *"odůvodněný
  strach z pronásledování z důvodu rasy, pohlaví…"* to *"způsobilost pro postavení uprchlíka
  v souladu s kapitolami II a III kvalifikačního nařízení"*; § 17 odst. 1 changed from
  grounds for **revoking** asylum to a right to apply for its **extension**; § 32 changed
  from the 15-day filing deadline to nothing but local jurisdiction. Every 2024 decision
  interpreting those provisions is interpreting text that no longer exists, and *nothing was
  ever said against those decisions* — which is exactly why no citation-based system finds
  this. Note what the cache key buys: 84 affected decisions collapse to **four** version
  pairs, so four model calls settle all of them. That is section 10's design paying off,
  measured rather than asserted.
- **Those 84 decisions currently show GREEN, and that is correct.**
  `provision_materiality` is empty and `ProvisionRepository` defaults `material` to
  **false**, because an un-judged rewording must never produce an amber — that would be a
  verdict with no evidence row behind it (rule 2). The verdicts cannot be hand-written
  either: rule 3 requires a validated `evidence_span` from an actual model response, and
  inventing one would fabricate the very evidence the design exists to guarantee. So the
  amber waits on `ANTHROPIC_API_KEY`, and the system says GREEN rather than guessing.
- **Schema limitation found while hunting for a red light: `provision` is one level too
  coarse for real derogations.** Constitutional Court nálezy are reachable and
  identifiable — an amending instrument whose `kodPodtypu` is `NALEZ` is a derogation, and
  its `nazev` names what it struck. Two real examples affecting acts this corpus cites:
  `130/2011 Sb.` (*Pl. ÚS 43/10*) struck **§ 33 odst. 3 věty první** of 150/2002, and
  `9/2010 Sb.` struck **§ 32 odst. 2 písm. a)** of 325/1999. Both are *partial* — a
  sentence, a lettered point. `provision` keys on `(act_no, section, subsec)` with no level
  below `subsec`, so recording either as `provision_version.derogated_by` would assert the
  whole subsection was struck down, turning every later decision that relies on the
  surviving text RED. That is the false red D8 rules out, so **neither was written**.
  Section 1's glossary is right that *derogace* means striking a provision **entirely**, and
  the schema models exactly that and nothing narrower. A partial derogation is, correctly, a
  rewording; the version timeline already carries it, and it should reach the user through
  the amber path rather than the red one. Widening the schema (a `point` column, or a
  `derogation_scope` on the version) is a real option, but it is a modelling decision, not a
  bug fix.
- **The timeline machinery, unchanged by the new source and now fed by it.**
  `build_timeline` closes validity windows to satisfy `ProvisionRepository`'s
  `VERSION_IN_FORCE` exactly (`valid_to` inclusive, so a window closes the day *before* the
  next opens), coalesces republished identical wordings so they never read as a rewording,
  preserves genuine gaps and refuses same-day starts. `jg/provisions.py` upserts
  id-stably — a version is matched by `valid_from`, so a `provision_materiality` foreign
  key survives a re-ingest — and refuses to delete a referenced version. The materiality
  call sits behind an injectable callable defaulting to `None`, so the stage runs with no
  API key. `esbirka.parse_dump` remains a loud `ParserSeam`: the live API is not a *dump*
  schema, nobody has seen that one, and a guessed key path would produce a confident AMBER
  on a perfectly sound decision. `jg-provisions.v1`, the format `parse_local_dump` reads,
  is an interchange format invented **for this repository** and is not an e-Sbírka schema.

- **The route from a citation to rows, as built and verified against the live API.**
  `esbirka.fetch_provision_versions(fetcher, act_no, sections, dates=…)` is M6's entry
  point and `provisions.ingest_act` is its database side; both go through the ordinary
  `Fetcher`, so the allowlist, robots, 1 req/s and the `data/raw/` cache come for free and
  nothing in `crawl/base.py` was touched. Four things had to be got right, and three of
  them are silent when got wrong:

  1. **`/historie` is the timeline, and two of its entries are not on it.** Alongside
     `AKTUALNI` and `MINULE` it returns `VYHLASENE` — the act *as promulgated*, starting at
     the publication date with **no end date at all** — and `MINULE_NEUCINNE`, the same text
     dated to the day before the act took effect. `VYHLASENE` is the dangerous one:
     `build_timeline` does not reject an open window, it *clamps* it, so leaving it in put
     the 2012 text in force from 2012 to 2020 and the ingest reported success. Both are
     filtered by `NON_TIMELINE_WORDING_KINDS`, and `fetch_provision_versions` additionally
     refuses any act whose selected wordings leave more than one window open, so an
     unfamiliar `typZneni` raises instead of being absorbed.
  2. **`datumUcinnostiZneniDo` is inclusive** — a window ends 2020-06-30 and the next opens
     2020-07-01 — which is already the convention `valid_to` and `VERSION_IN_FORCE` use, so
     it is stored as-is rather than shifted.
  3. **`/fragmenty?cisloStranky=N` is 0-based.** Page 1 starts at § 309 of the civil code
     and page `pocetStranek` is an HTTP 400, so a 1-based loop drops §§ 1–308 with no error
     anywhere. Pinned by `test_the_first_fragment_page_is_page_zero`.
  4. **`/obsah` is not used for wording text.** Its `textUstanoveni` is truncated at ~250
     characters, and a body cut mid-sentence makes two identical wordings compare unequal.
     `/fragmenty` carries the full `xhtml`; `<var>(1)</var>` is kept because it is part of
     how the provision reads and `<czechvoc-termin>` wrappers are dropped.

  The provision key is read out of a fragment's **ELI path** (`…/par_1180/odst_1`), not out
  of its printed citation, because the path is the API's own structural key and cannot
  disagree with itself; a `pism_` folds into the odstavec above it, since `provision` has
  no column below `subsec` and *§ 1170 odst. 2* is how such a rule is cited anyway. The act
  number stored is read back off `/detail-zneni`'s `citace` rather than echoed from the
  request, per rule 1. Each § yields both a `subsec = null` row (heading plus every
  odstavec) and one row per odstavec, because a citation may name either.

- **M6 has real data. Verified against Postgres, not asserted.** `89/2012` §§ 1180 and 2000
  are loaded across all nineteen effective wordings: 108 fetched version records collapse
  to 8 `provision_version` rows over 6 `provision` rows, windows ascending and
  non-overlapping, `valid_to` null on exactly one row per provision,
  `timeline_problems` empty for all six. § 1180 odst. 1 is the section 10 case end to end —
  in force at a 2016 `decided_on` it reads *"Nebylo-li jinak určeno, přispívá vlastník
  jednotky na správu domu a pozemku ve výši odpovídající jeho podílu…"*, and as of today
  *"Vlastník jednotky přispívá na správu domu a pozemku v poměru odpovídajícím jeho
  podílu…, nebylo-li v prohlášení určeno jinak…"*, reworded with effect from 2020-07-01 by
  163/2020 Sb. § 2000, which nothing ever amended, collapses nineteen identical records to
  one open row — the control that proves a republication does not read as a rewording. A
  second ingest reports 0 inserted, 0 updated, 0 deleted with every row id unchanged, and
  issues zero network requests: the API tests drive a real `Fetcher` over an HTTP client
  that raises on any call, so cache replay is not asserted, it is the only way they pass.

- **`esbirka.crawl()` still refuses, deliberately.** A statute source has nothing to
  enumerate — the API is addressed by act, and which acts to load follows from the
  citations already extracted — so `jg crawl ESBIRKA` names the working route
  (`python -m jg.provisions fetch --act … --section … --all-wordings`) instead of pretending
  to walk the Sbírka. Wiring a per-act fetch into `jg crawl` would need `jg/cli.py` and
  `jg/crawl/__init__.py`, which this change deliberately left alone; the stage is reachable
  as `python -m jg.provisions`, which also gained `fetch` and `compare`. Worth a `make
  provisions` target once the CLI owner can take it.

### Corpus: the NSS source, and the allowlist decision behind it

**`vyhledavac.nssoud.cz` was added to the allowlist on explicit user authorisation on
2026-09-02**, recorded in CLAUDE.md rule 6 and in `config.ALLOWLISTED_HOSTS`. It had to be
a human decision, because rule 6 says stop and ask. The case for it: the allowlisted
`www.nssoud.cz` publishes no decision text whatsoever, so NSS — the court M1 is defined
around, and the one whose rozšířený senát referral mechanism gives the project its free
`DEPARTED` signal — was otherwise uncrawlable, and M1 through M4 were unreachable by any
honest route. `vyhledavac.nssoud.cz` serves no `robots.txt` (404) and is a subdomain of
the `nssoud.cz` that rule 6 already named.

**The search protocol, verified live before any parser was written.** It is an ASP.NET Core
MVC app, not a static site, so the route is worth recording:

1. `GET /` returns one `<form>` with ~299 inputs; the session needs its
   `.AspNetCore.Antiforgery.*` cookie and the `__RequestVerificationToken` field.
2. The date range lives at
   `vyhledavaciSekce[1].vyhledavaciPodminka[0].vyhledavaciPodminkaHodnota[0]` with
   `.HodnotaDatumACasOd` / `.HodnotaDatumACasDo`, Czech-formatted (`1.1.2024`), plus
   `btSubmit`.
3. `POST /Home/Index?formular=1&zobrazeniVysledkuVolba=5`. The `zobrazeniVysledkuVolba=5`
   is load-bearing: it selects the **ECLI result view**, which means the crawler reads the
   authoritative ECLI off the page instead of deriving it from the case number. Deriving it
   would be inventing an identifier, which rule 1 forbids outright.
4. Results carry date, `č. j.`, `Soud (senát)` (the panel-type signal — *tříčlenný senát*
   versus a named *rozšířený senát*), `Druh dokumentu`, `Výrok rozhodnutí` (the operative
   outcome the structural `QUASHED` rule reads), ECLI, and a document id. Total is in the
   page text as `Počet nalezených záznamů: N` — 912 for January 2024 alone.
5. Paging is `POST /Home/MyResTRowsCont` with `vyhledavaciPodminky`,
   `zobrazeniVysledkuId`, `pageNum`, `resultOrder`, taken from inline script vars on the
   result page. An empty body means the end.
6. Full text is `GET /DokumentOriginal/Html/{id}`, carrying `[1]`, `[2]`… paragraph
   numbering that maps straight onto `decision_paragraph.idx`, so a cited
   `paragraph_idx` is something a lawyer can actually look up. The sibling
   `/DokumentOriginal/Text/{id}` is UTF-16 and must not be used naively.

Requiring a cached POST is why `Fetcher` gained one; the cache key is a hash of the URL
plus the canonical payload, with the payload recorded in the sidecar so a cached POST stays
auditable. The per-session `__RequestVerificationToken` is excluded from the key, or no
two runs would ever share a cache entry.

**A stated limit: the site's paging is not deterministic.** `currSort` ties on nearly every
row and each page request is a fresh query execution, so a page walk both duplicates and
drops rows. Two independent walks of 31 January 2024 (128 records) each returned 128 rows
but only 87 and 83 *distinct* ECLIs. The crawler therefore chunks by day — most days fit
inside the 40-row first page and are exact in a single request — and re-walks any day that
needs paging up to four times under different cache salts, unioning by ECLI. Measured on 30
January 2024 that recovered 50, then 51, then 55 of 58. **The remaining shortfall is
reported, not hidden**: `CrawlReport.short_days` carries `{day: (reported, seen)}` and each
one is logged. This is the honest version of the coverage caveat section 16 already
commits to — the corpus has holes, and the system says so rather than implying completeness.

Rows for regional administrative courts appear in the same result set and are skipped at
row-parse time, before any document fetch, because `decision.court_code` is a foreign key
onto the three seeded courts. Skipping them costs nothing: 28 document fetches produced 27
stored decisions in the verification window.

**Routes that were investigated and rejected**, recorded so nobody re-treads them:

- **`www.nssoud.cz` open data.** One spreadsheet,
  `/fileadmin/user_upload/dokumenty/Otevrena_data/Data_2026/Srpen_2/otevrena_data_NSS.xlsx`,
  16.7 MB, covering 2003 onwards, last updated 17 Aug 2026. Metadata only: it would fill
  `decision`, `decision_alias` and `corpus_meta` — enough to make citation *resolution*
  real and to state the scoped assertion of section 2 with a true N — but not
  `decision_paragraph`, so no citation could be extracted from it and no `treatment` row
  could ever be produced. It also needs an xlsx reader. Superseded by the search route
  above, which yields the full text. Its `robots.txt` permits crawling and declares
  `Crawl-Delay: 10`, which the `Fetcher` honours over the 1 s default.
- **NS is closed.** `rozhodnuti.nsoud.cz/robots.txt` is `Disallow: /` for `User-agent: *`
  and allows only the Ministry's own `DG_JUSTICE_CRAWLER`. Rule 5 says honour robots.txt,
  so `crawl()` refuses eagerly with `CrawlUnavailable` rather than guessing at markup
  nobody has observed. This one is not a technical obstacle and cannot be worked around;
  it would need the court's permission.
- **ÚS is partially open, and the obstacle turned out not to be enumeration.** NALUS search
  is indeed a stateful ASP.NET postback with no iterable list of document keys — but
  enumeration is not needed, because the text URL encodes the case number and the corpus
  tells us exactly which 753 ÚS decisions it cites. The real blocker is that NALUS publishes
  no ECLI and `decision.ecli` is the primary key. See "The ÚS corpus" below.

Consequence for the eval artefacts: they were built before any corpus existed and each one
says so rather than carrying placeholder numbers. `eval/report.py` always exits 0 and
refuses to score any row bearing a `TEST-` identifier, so it can be run before the data
exists without producing a fake score. The 50 gold labels in `eval/labels.csv` and the
30-document extraction sample are human work that only becomes possible once NSS rows are
loaded.

### Extraction, measured against the real corpus

- **The `ecli` pattern has zero real-world hits.** Across all 145,990 crawled paragraphs
  there is not one `ECLI:CZ:` string — NSS cites exclusively by `sp. zn.` and `č. j.`.
  `journal_no` is nearly as idle: 25 matches, 0 resolved, because the R-číslo belongs to the
  Supreme Court's reporter and NSS rarely uses it. Both patterns stay (they cost nothing and
  an uploaded document may well carry an ECLI), but section 7's implicit assumption that
  ECLI is a usable *extraction* target is wrong for this corpus. It remains the right
  primary **key**; it is just never the thing you find in the text.
- **What actually fails to resolve, from a 40,000-paragraph sample.** Of 13,397 decision
  references, 8.1% resolve. Of the rest: **86% point at other NSS decisions**, 10% at ÚS
  decisions, and only 3% at regional courts. So corpus width is the dominant lever and it is
  a lever worth pulling — going from 4 months to 16 doubled `case_no` and `ref_no`
  resolution. The case-number *filing* years cluster at 2019–2021, which means the cited
  decisions themselves sit in 2020–2022.

### The ÚS corpus: enumeration is not the blocker, the primary key is

Section 18 previously recorded NALUS enumeration as the ÚS blocker. That is no longer the
obstacle, and the real one is sharper.

- **Cited ÚS decisions can be fetched directly, no enumeration needed.** The NALUS text URL
  encodes the case number: `GetText.aspx?sz={panel}-{number}-{yy}_1`, where the panel is the
  Roman numeral mapped to a digit (`I`→1 … `IV`→4) and the plenum is the literal `Pl`.
  Verified live on five: `II. ÚS 2379/08` → `2-2379-08_1`, `III. ÚS 989/08` → `3-989-08_1`,
  `I. ÚS 741/06` → `1-741-06_1`, `Pl. ÚS 44/21` → `Pl-44-21_1`, `IV. ÚS 3523/20` →
  `4-3523-20_1`. Since the corpus cites **753 distinct ÚS case numbers** (2,154 mentions),
  a targeted fetch of exactly the cited decisions is both possible and far cheaper than
  crawling the court.
- **The structural `QUASHED` signal is really there.** `IV. ÚS 3523/20` (24. 8. 2021,
  N 144/107 SbNU 211) has the operative part, after `takto:`, *"Rozsudkem Nejvyššího
  správního soudu ze dne 29. října 2020 č. j. 5 Afs 470/2019-33 a usnesením Krajského soudu
  v Brně … se ruší."* That is an ÚS výrok annulling a named NSS decision — a red light that
  needs **no model call at all**, only the structural rule from section 8. The annulled
  decision was decided 2020-10-29 and falls inside the 2020–2022 crawl window.
- **The blocker: NALUS publishes no ECLI, and `decision.ecli` is the primary key.**
  `GetText.aspx` contains no `ECLI:` string anywhere; `ResultDetail.aspx` needs a session and
  302s; and the crawled NSS corpus never cites an ECLI either. So there is no authoritative
  ÚS ECLI within reach, and rule 1 forbids constructing one. The ÚS ECLI scheme is
  deterministic and the NSS ECLIs already stored follow exactly the same shape — but those
  are *read off the page*, not derived, and deriving one risks emitting an
  authoritative-looking identifier that is subtly wrong (the trailing sequence number, and
  cases with more than one decision). That is a rule 1 judgement for a human, not a call to
  make in passing. Until it is settled, ÚS decisions are not stored and the structural red
  light stays out of reach.

### Frontend, verified against the real corpus

Checked in a browser against the live API and the 5,744-decision corpus, not just built.
The scope header renders section 2's assertion verbatim — *"Posouzeno ke dni 4. 9. 2026
proti korpusu 5 744 rozhodnutí (NSS) zveřejněných do 30. 4. 2024"* — followed by section
16's caveat that the result speaks only to that corpus and that not every decision is
published. Resolved sources show which ECLI they were matched to. The *Nepřiřazené odkazy*
panel says outright *"Netvrdíme o nich nic — nedostaly zelené ani červené světlo"*, which is
the honest treatment section 11 demands of the `unresolved` field. Expanding a non-green row
gives the label in Czech and in the enum, the **verbatim evidence span**, the citing
decision, its `Rozhodovací těleso` (so the reader sees *why* an extended panel could depart)
and the paragraph number; a red row lists its amber-grade reasons underneath, matching
`StatusEngine`. The provision row shows the change date, whether the change is *věcná*, and
tells the reader to compare both wordings themselves. Demo-mode spans are prefixed
`UKÁZKOVÁ DATA.` and every demo identifier is `TEST-` prefixed, so mock output can never be
mistaken for a finding.

### The first red light, and the false red found on the way to it

The ÚS corpus landed: **2,754 Constitutional Court decisions**, fetched one at a time by
case number through the URL mapping above, keyed on the NALUS document key
(`decision.ecli = "nalus:4-3523-20_1"`) per the user's decision. `V5__decision_key_convention
.sql` records that convention in the schema as column comments — no DDL, no data change,
because Flyway must stay the one place the schema's meaning is written down. That produced
the first **RED light in real data, with no model call**: 25 `QUASHED` rows over 5 genuine
annulment pairs, structural route, confidence 1.0. Verified end to end — a document citing
`č. j. 5 Afs 470/2019-33` comes back RED, *zrušeno*, evidenced by the verbatim výrok of
`IV. ÚS 3523/20`. M3's "done when" is met.

**But the first run produced 38 QUASHED rows, and 4 of the 9 distinct pairs were false.**
Worth recording in full, because the failure was silent and the mechanism is general.

- *The symptom.* The annulled decision **postdated its own annulment**: `I. ÚS 2164/17`
  (2018-10-25) was reported as having quashed a decision from 2020-05-28. Logically
  impossible, and it took a date comparison to see — every individual component looked right,
  including a verbatim výrok naming the case number.
- *The cause.* `alias_candidates` resolved a `č. j.` with a sheet number by trying the full
  form first and then **falling back to the bare spisová značka**. A case number is not
  unique to a decision: one case yields several decisions over the years, each with its own
  sheet number. The cited `3 As 205/2016-38` (2017, annulled) fell back to `3 as 205/2016`,
  which the corpus had registered against `3 As 205/2016-63` — the decision the same court
  issued **in 2020 on remand, after the annulment**. So the system took the current, valid
  successor decision and marked it dead, on the strength of the annulment of its predecessor.
  That is a false red, the one error D8 says is unacceptable, and it arrived pointing at
  exactly the decision a user would most want to rely on.
- *Two fixes, because one was not enough.* The fallback is gone: a `č. j.` carrying a sheet
  resolves only on the full form, and a lost match is a coverage gap the product already
  states openly. And `classify_structural` now refuses any annulment that is not
  chronological — the cited decision must strictly predate the citing one, and a missing date
  is a refusal rather than an assumption. The guard is redundant with the resolver fix today
  and deliberately kept: it is an absolute invariant about what a court can do, it costs one
  comparison, and it catches this whole class however a reference came to be misresolved.
- *The blast radius.* 278 of 7,024 resolved decision citations — **4%** — pointed at the
  wrong decision of the right case. Those rows and their treatments were deleted rather than
  left to rot; QUASHED fell from 38 to 25 and every surviving pair is chronologically sound.
- *Why 4% concentrated on red.* This error is not uniform. A case acquires a second decision
  precisely *because* the first was annulled, so the very cases that carry an annulment are
  the ones most likely to have two decisions to confuse. A rare-looking resolution bug landed
  almost entirely on the highest-stakes verdict the system produces.

**Reasons are now deduplicated in `StatusEngine`.** One decision commonly cites another in
several paragraphs — `IV. ÚS 3523/20` names the annulled judgment in paragraphs 2, 3 and 4 —
which is three `citation` rows, three identical treatment rows, and was three identical
entries in the evidence panel, all quoting the one výrok. `Reason` records are values, so
equal ones are the same fact; the list is now distinct while preserving table order.

### Two model providers, because neither subscription includes API access

Section 4 names one model provider. There are now two, and the reason is commercial rather
than technical: **a Claude Team plan and a Google Pro subscription are both seat products,
and neither grants API access.** The Anthropic API bills separately from
`console.anthropic.com`; Google AI Pro likewise does not raise Gemini API quota. Google AI
Studio does publish a genuinely free API tier, so Gemini is the provider this project can
actually be run on at zero cost. Anthropic is untouched and remains selectable.

Measured before choosing, rather than estimated after: the queued M5 workload is **1,583
edges, 10,451,203 prompt characters, ~3.0–3.5M input tokens**. On the Anthropic API that
is roughly $5 at Haiku 4.5 or $11 at Sonnet 5, halved again by the Batch API — cheap, but
not free.

- **The seam made this a small change, which is the payoff of D2.** Both runtimes already
  expressed the model as one narrow contract — `ModelCall = Callable[[str], Mapping]` in
  Python, `LlmClient.complete(String) -> String` in Java. Everything above it is
  provider-agnostic: prompt assembly, the evidence-span gate, the single retry, the response
  cache, the `UNCLASSIFIED` fallback. A provider is a transport and is never a second place
  where labels are judged, so a cheaper model cannot weaken the safety story — rule 3 still
  rejects any span that is not a literal substring of the supplied context.
- **The wire format is the Interactions API, not `generateContent`.** Google replaced the
  `models/{model}:generateContent` + `contents`/`parts` shape with
  `POST /v1beta/interactions`: a flat `{model, input}` body, the key in an `x-goog-api-key`
  **header** (never a `?key=` query parameter, which would put a credential in proxy logs
  and exception messages), `temperature` and `thinking_level` nested in `generation_config`,
  JSON constrained by `response_format`, and the reply in a `steps` timeline whose
  `model_output` step carries the text. Verified against ai.google.dev on 2026-09-05.
- **Rule 7 is honoured more literally here than on Anthropic.** The Anthropic client
  documents a deviation: current Claude models removed the sampling parameters and reject a
  request carrying one, so it sends no `temperature` at all. Gemini still accepts it, so the
  Gemini path sends `temperature: 0` explicitly.
- **The response parser is deliberately defensive.** Google documents the SDK's `output_text`
  convenience property, not the raw wire shape, so the parser prefers the documented
  `model_output` step, falls back to `output_text`, and otherwise raises naming the status,
  the keys it saw and a truncated body. It never returns an empty string: that would reach
  the span gate as a malformed answer and report a *model* failure when what actually
  happened is that the wire format moved.
- **Free-tier limits shape the design.** They are enforced per Google Cloud project on
  requests/minute, tokens/minute and requests/day simultaneously, and breaching any one
  returns 429. Reported figures are ~10–15 RPM and ~1,000–1,500 RPD, so 1,583 calls is
  several hours and probably spans the daily cap. Hence two separate mechanisms: proactive
  **pacing** (a minimum 6 s gap, `JG_GEMINI_MIN_INTERVAL`, set 0 on a paid tier) so the
  batch mostly never provokes a 429, and **retry** with `Retry-After` and jittered backoff
  for the axes pacing cannot address. A `Retry-After` over 300 s is treated as a daily cap
  and fails loudly rather than sleeping for hours. The existing response cache means a
  resumed run does not re-pay for edges already classified.
- **Provider selection is one environment decision.** `JG_LLM_PROVIDER` wins if set,
  otherwise whichever key is present, defaulting to Anthropic so the no-key path is
  unchanged. `treatment.model` and `provision_materiality.model` record the real model ID,
  so a corpus holding rows from both providers stays unambiguous about which produced what.
- **`ModelCall` and `ModelUnavailable` moved to `jg/llm_types.py`.** They lived in
  `jg.classify.reasoning`, which meant `jg.gemini` importing them put an edge into the
  `jg.classify` package, whose `__init__` imports `router`, which imports `jg.gemini`. The
  cycle never failed under pytest — collection order always imported `jg.classify` first —
  but `import jg.gemini` as the first import of a fresh interpreter died. A leaf module with
  no `jg` imports cannot take part in a cycle at all, and `tests/test_imports.py` now
  imports every module first in its own subprocess, because doing it in one process would
  hide the next such bug exactly as the suite hid this one.

### A long batch is not one unit of work

The first real Gemini run hit the free tier's daily cap and **lost everything it had
bought**. `jg.db.connect()` commits on clean exit and rolls back on any exception — correct
for an atomic unit of work — but `run_classify` wrote every treatment inside that one
transaction and never committed mid-loop. When the terminal 429 propagated out of the
`with` block, the rollback took not only the model-classified edges but the structural and
triage rows written earlier in the same run. Confirmed after the fact: `llm_cache` was
empty, so not one paid answer survived. On a 500-request daily cap that is a whole day of
quota spent for nothing, and re-running would have repeated it forever — the batch could
never outlive a single quota window, so it could never finish at all.

Three changes, and the first is the one that matters:

- **Both batch loops commit per item.** `run_classify` and `run_materiality` each commit
  after every edge or pair. A batch of independent model calls is not one unit of work; each
  item is. This is what makes "run it again tomorrow" actually converge, and it is what lets
  the runner honestly claim the work is saved. Both stages already re-query only unfinished
  work (`load_edges` skips edges that have a treatment, `pending_rewordings` skips judged
  pairs), so durability was the only missing half of resumability.
- **`QuotaExhausted` is its own error.** A 429 that outlives every retry is not a model
  failure, it is the end of the budget, and the operator's next action is different: change
  nothing, wait for the window, run the same command. It subclasses `ModelUnavailable`, so
  callers that catch the parent are unaffected, and it carries the provider's own
  `retry_after` hint.
- **The runner stops cleanly instead of raising.** A traceback out of `make classify` told
  the operator nothing about how far the run got or whether anything survived. It now reports,
  in Czech: how many edges were classified, how many remain, the provider's suggested wait,
  and that re-running resumes.

Measured limit, which is lower than the public write-ups suggest: the API reported
`limit: 500` per day for `gemini-3.5-flash-lite`, not the 1,000–1,500 those sources quote.
At 500/day the 1,583-edge queue is roughly four sessions. The per-minute pacing does not
help with a daily cap — nothing does except coming back — which is precisely why the work
has to be durable.

### Build and environment

- **Postgres is on host port 55432**, not 5432, to stay clear of a system Postgres. Both
  runtimes default to it (`application.yml`, `Makefile`, `pipeline/jg/config.py`).
- **Spring Boot 4.1.1 no longer manages Testcontainers versions**, so `testcontainers-bom`
  is imported in `dependencyManagement`.
- **Flyway is pinned to 13.4.0**, newer than Boot 4.1 manages, so that the runtime
  migration on startup and the `flyway-maven-plugin` behind `make migrate` agree on one
  version.
- **`spring.persistence.exceptiontranslation.enabled: false`.** It exists to translate
  exceptions from native persistence APIs, and there are none here; `JdbcClient` already
  surfaces `DataAccessException`. Leaving it on would put a CGLIB proxy around every
  repository and force them all to stop being `final`.
- **`tomlj` is the one dependency added beyond section 4's list.** Java has no TOML parser
  in the JDK, and the alternative to reading `patterns.toml` is duplicating the regexes,
  which is the thing section 7 exists to prevent.
- **The toolchain is vendored into `.tools/` by `make toolchain`** (Temurin JDK 25 and
  Maven 3.9), so the build needs no system package manager. `.tools/` is gitignored.
- **The Makefile grew past section 4's list**: `toolchain`, `down`, `clean-db`, `psql`,
  `migrate-info`, `venv`, `web-install`, `eval-extract`, `test-java` and `test-python`, per
  CLAUDE.md's rule about adding a target for anything run twice. The CLI likewise gained
  `jg status` (per-court row counts and coverage) as a read-only companion to the stage
  commands.

### Milestone status, measured rather than asserted

Numbers below were produced by running the pipeline, not by estimating it.

| # | State | Evidence |
|---|---|---|
| **M0** | **done** | `make up && make migrate && curl /api/health` → 200. V1–V4 applied. |
| **M1** | **done, two courts** | **18,458 NSS decisions** (2020-01-06 → 2024-04-30) plus **2,754 ÚS decisions** — 21,212 in all, 553,659 paragraphs, 64,123 aliases. The ÚS side is a *targeted* load: only the decisions the corpus actually cites, fetched by case number, since NALUS needs no enumeration once you can build the URL. A re-run issues zero HTTP requests, proven the hard way — Postgres died mid-crawl and the resume replayed 291 cached days in under a minute before continuing. |
| **M2** | **partial** | 173,655 references, 90,877 citations, 11,340 provisions created. Resolution 56.9% overall — and widening the corpus from 4 months to 16 more than doubled decision-to-decision resolution (`case_no` 3.5% → **7.3%**, `ref_no` 3.9% → **9.3%**), while `provision` held at ~78%. The 30-document hand-checked sample the milestone asks for still does not exist; that is human work. |
| **M3** | **done** | **A genuine red light exists in the data**, produced with no model call: 25 `QUASHED` rows over 5 real annulment pairs, structural route, confidence 1.0. `ECLI:CZ:NSS:2020:5.Afs.470.2019.33` returns RED / *zrušeno*, evidenced by the verbatim výrok of `IV. ÚS 3523/20`. Section 8 budgeted ~7.5% of edges for the model; measured **4.3%**. Everything else is still `FOLLOWED`/`MENTIONED` — `DEPARTED` and `NARROWED` need the reasoning tier. |
| **M4** | **done** | Pasting a document citing `č. j. 5 Azs 120/2023-24` and `sp. zn. 9 Ao 37/2021` resolves both to real ECLIs and returns lights plus the scoped Czech verdict. |
| **M5** | **not started** | Router tiers 2–3 are built and tested; the 22 escalated edges wait on `ANTHROPIC_API_KEY`. |
| **M6** | **partial — detection done at scale, judgement blocked** | Real statutory data for 89/2012, 325/1999 and 150/2002: 249 `provision_version` rows, windows non-overlapping. Section 10's scenario is **reproduced on real law at corpus scale**: **687 of 5744 decisions (12%) rely on a provision that has since been reworded**, and settling every one of them costs **18 model calls** — a 38:1 payoff from keying materiality on the version pair. Biggest single exposures: `150/2002 § 60 odst. 3` (327 decisions), `§ 46 odst. 1` (251), `325/1999 § 12` (211). All 687 correctly read GREEN today, because materiality is unjudged and defaults to false rather than guessing. |
| **M7** | **not started** | `PropositionChecker` and its prompt exist and are tested against stubs. |
| **M8** | **blocked on human work** | `eval/labels.csv` has 0 gold rows. Section 15 requires them hand-labelled with the decision text open; fabricating them would violate rule 1 and make every number meaningless. |

**What is and is not reachable without a model, stated plainly.**

*Red is reachable, and reached.* `QUASHED` is pure metadata — an ÚS výrok annulling a named
decision — so it needs no inference. Loading the cited ÚS decisions was what unlocked it;
before that the corpus was NSS-only and NSS mostly annuls regional-court decisions, which
are filtered out at row-parse time because `decision.court_code` admits only the three
seeded courts.

*The rest of the spectrum is not.* `DEPARTED` and `NARROWED` are readings of reasoning, and
every edge that might carry one is exactly what the router escalates to the reasoning tier.
`ProvisionReworded` amber needs a materiality verdict on the version pair, and rule 2 forbids
an amber with no evidence row behind it, so `material` stays false until a model judges it.

So: the **detection** layer works on real data at real scale, the **structural** verdicts are
live, and the **inferential** ones are one API key away. Widening the crawl still helps the
citation graph — 4 months to 16 doubled decision-to-decision resolution — but on its own it
buys coverage, not new kinds of verdict.

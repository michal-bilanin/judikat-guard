-- Judikát Guard core schema. See PLAN.md section 6.
-- Flyway owns the schema; the Python pipeline reads and writes rows only.

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
  label          text not null,           -- see PLAN.md section 8
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

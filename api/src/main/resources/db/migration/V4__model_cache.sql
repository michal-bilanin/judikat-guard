-- Model-call caching and the provision-materiality evidence table.
-- PLAN.md section 8 (cache treatment classifications) and section 10 (cache materiality
-- judgements on the version pair, independently of which decision triggered them).

-- Generic response cache. cache_key is built by the caller from the key documented for
-- each prompt, e.g. treatment: (citing_ecli, cited_ecli, paragraph_idx, prompt_version).
-- Temperature is always 0, so a hit is a faithful replay rather than an approximation.
create table llm_cache (
  cache_key      text primary key,
  prompt_version text not null,
  model          text not null,
  request        jsonb not null,
  response       jsonb not null,
  created_at     timestamptz not null default now()
);

-- The materiality verdict on a provision rewording. Keyed on the version pair so that the
-- many decisions relying on the same provision share one judgement. Carries its own
-- evidence_span for the same reason treatment does: no verdict without a traceable quote.
create table provision_materiality (
  from_version_id bigint  not null references provision_version(id),
  to_version_id   bigint  not null references provision_version(id),
  prompt_version  text    not null,
  material        boolean not null,
  confidence      numeric(3,2) not null,
  evidence_span   text    not null,
  model           text,
  created_at      timestamptz not null default now(),
  primary key (from_version_id, to_version_id, prompt_version)
);

-- Remove pgvector. See PLAN.md section 18.
--
-- `decision.ratio_embedding vector(1536)` was written into V1 with the rest of the schema,
-- for the "ratio retrieval and near-duplicate detection" named in PLAN.md section 4. It was
-- never populated: no milestone M0..M8 asked for it, no code in either runtime read or wrote
-- it, and the column stood null on all 21,212 rows. Its input never existed either — both
-- crawlers set `ratio_summary=None`, so there was no právní věta to embed in the first place.
--
-- Two changes, because one alone does not finish the job:
--
--   * V1 no longer declares the column or `create extension vector`, so a *fresh* database
--     installs on stock Postgres. That is the point of removing it: pgvector is an extension
--     a managed host has to offer, and requiring one for a column nothing reads is a
--     deployment constraint bought with nothing.
--
--   * This migration drops both from a database that already ran the old V1. `if exists`
--     makes it a no-op on a fresh install, so the two paths converge on the same schema.
--
-- Editing an applied migration needs `flyway repair` to re-checksum V1 (`make migrate-repair`).
-- That is the price of not leaving a create-it-then-drop-it pair in the history, which would
-- keep the pgvector requirement alive at install time for no reason.
--
-- Re-adding this later costs one migration. `ratio_summary` is the real prerequisite, and it
-- is a crawler change, not a schema change.

alter table decision drop column if exists ratio_embedding;

drop extension if exists vector;

-- The convention behind decision.ecli, recorded where the schema itself can be read.
--
-- No DDL, no data change, no new column: this migration exists purely so that the rule
-- governing the primary key lives in the schema Flyway owns (CLAUDE.md rule 4) rather than
-- only in a Python docstring. `\d+ decision` in psql now answers the question directly.
--
-- Why the convention is needed. PLAN.md section 6 keys `decision` on the ECLI, and for NSS
-- that works: the vyhledavac.nssoud.cz ECLI result view prints the authoritative ECLI, so
-- the crawler reads it off the page. The Constitutional Court's NALUS publishes no ECLI at
-- all -- there is no "ECLI" substring anywhere on Search/GetText.aspx, ResultDetail.aspx
-- 302s without a session, and the crawled NSS corpus never cites one either (zero hits
-- across the whole paragraph table). CLAUDE.md rule 1 forbids constructing one: the ÚS
-- scheme is deterministic in outline, but the trailing sequence number and cases carrying
-- more than one decision make a derived ECLI an authoritative-looking identifier that can
-- be subtly wrong. See PLAN.md section 18, "The ÚS corpus".
--
-- What is stored instead: the source's own document key, namespaced so it can never be
-- mistaken for an ECLI. For NALUS that is the `sz` parameter the full-text URL is
-- addressed by, e.g. `nalus:4-3523-20_1` for IV. ÚS 3523/20. That string is crawled data --
-- it is what NALUS itself answers to -- so rule 1 holds.
--
-- Citations never resolve through this column. Resolution goes through `decision_alias`,
-- which carries the case number in both spellings seen in the wild ("IV. ÚS 3523/20" and
-- "IV.ÚS 3523/20") and the SbNU citation where the decision has one.

comment on table decision is
  'One court decision. The primary key is the ECLI where the source publishes one and a '
  'namespaced source document key otherwise; see the comment on decision.ecli.';

comment on column decision.ecli is
  'Primary key. Holds the authoritative ECLI when the source publishes one (NSS, read off '
  'the vyhledavac.nssoud.cz ECLI result view), and otherwise a namespaced source document '
  'key of the form "<source>:<key>" -- currently "nalus:<sz>", e.g. "nalus:4-3523-20_1", '
  'for Constitutional Court decisions, because NALUS publishes no ECLI and CLAUDE.md rule '
  '1 forbids deriving one. Either way the value is read from the source, never constructed. '
  'A namespaced key always contains a colon before the first digit group, so the two forms '
  'are distinguishable by prefix ("ECLI:CZ:" vs "nalus:").';

comment on column decision.case_no is
  'Spisova znacka, exactly as the source prints it. NALUS prints "IV.US 3523/20" with no '
  'space after the numeral while citing decisions write "IV. US 3523/20"; both spellings '
  'are written to decision_alias so either resolves.';

comment on column decision.journal_no is
  'Official reporter citation, null when unpublished (the normal state). R-cislo for the '
  'Supreme Court; the SbNU parallel citation ("N 144/107 SbNU 211") for the Constitutional '
  'Court. A nalez promulgated in the Sbirka zakonu also prints an act number such as '
  '"38/2023 Sb." next to it; that is deliberately not stored here, because it is an act '
  'number rather than a reporter citation.';

comment on column decision_alias.alias is
  'Normalised by jg.extract.resolver.normalize_alias (unicode spaces collapsed, trimmed, '
  'case-folded, punctuation untouched) on both the write and the read side.';

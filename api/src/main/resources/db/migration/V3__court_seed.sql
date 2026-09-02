-- The three courts whose case law this system tracks. See PLAN.md section 1.
-- Codes match the citing_court / cited_court values seeded in V2.

insert into court (code, name) values
  ('US',  'Ústavní soud'),
  ('NSS', 'Nejvyšší správní soud'),
  ('NS',  'Nejvyšší soud');

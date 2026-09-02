-- Which body may depart from which court's case law. See PLAN.md section 6 (D5).
-- Seeded data rather than code so the legal rules stay inspectable and adjustable.

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

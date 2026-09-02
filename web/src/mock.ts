/**
 * Sample report for the "ukázková data" toggle, so the page renders with the API down.
 *
 * Hard rule 1 (CLAUDE.md): no invented identifiers. Everything here is either literally
 * present in PLAN.md / extract/patterns.toml, or carries a `TEST-` prefix that cannot
 * resolve against the corpus. The evidence spans and the rewording date are placeholder
 * prose attached to `TEST-` decisions; the UI labels the whole report as sample data.
 */

import type { DocumentReport } from './types';

export const MOCK_REPORT: DocumentReport = {
  asOf: '2026-09-01',
  corpus: {
    NSS: { count: 3142, through: '2026-08-15' },
  },
  sources: [
    {
      // The example row of PLAN.md section 11.
      ecli: 'ECLI:CZ:NSS:2015:6.Ads.45.2014.32',
      rawText: 'rozsudek NSS ze dne 12. 3. 2015, č. j. 6 Ads 45/2014-32',
      light: 'AMBER',
      reasons: [
        {
          kind: 'NARROWED',
          byEcli: 'TEST-ECLI-NSS-NARROWED',
          span:
            'UKÁZKOVÁ DATA. Závěr vyslovený v citovaném rozsudku se uplatní pouze tam, ' +
            'kde účastník řízení unesl břemeno tvrzení již v řízení před správním orgánem.',
          paragraph: 34,
        },
      ],
    },
    {
      ecli: 'TEST-ECLI-NSS-SUPERSEDED',
      rawText: 'rozsudek NSS, č. j. TEST-1 Afs 1/2001',
      light: 'RED',
      reasons: [
        {
          kind: 'SUPERSEDED',
          byEcli: 'TEST-ECLI-NSS-EXTENDED',
          panel: 'extended',
          span:
            'UKÁZKOVÁ DATA. Rozšířený senát na dosavadním výkladu nesetrvává a odchyluje ' +
            'se od něj v celém rozsahu.',
          paragraph: 41,
        },
        {
          kind: 'NARROWED',
          byEcli: 'TEST-ECLI-NSS-NARROWED',
          span: 'UKÁZKOVÁ DATA. Uvedený závěr dopadá jen na řízení zahájená po účinnosti novely.',
          paragraph: 12,
        },
      ],
    },
    {
      // A statutory provision, not a decision: no ECLI. The reference form is the one
      // documented in extract/patterns.toml.
      ecli: null,
      rawText: '§ 2000 odst. 1 zákona č. 89/2012 Sb.',
      light: 'AMBER',
      reasons: [
        {
          kind: 'PROVISION_REWORDED',
          provisionId: 1,
          changedOn: '2017-01-01',
          material: true,
          span:
            'UKÁZKOVÁ DATA. Znění účinné v době rozhodnutí a znění účinné dnes se liší ' +
            'v podmínce, o kterou se odůvodnění opíralo.',
        },
      ],
    },
    {
      ecli: 'TEST-ECLI-NSS-UNCLASSIFIED',
      rawText: 'rozsudek NSS, sp. zn. TEST-2 As 2/2002',
      light: 'AMBER',
      reasons: [
        {
          kind: 'UNCLASSIFIED',
          byEcli: 'TEST-ECLI-NSS-CITING',
          span: 'UKÁZKOVÁ DATA. Text, u něhož se klasifikace dvakrát nezdařila.',
          paragraph: 7,
        },
      ],
    },
    {
      ecli: 'TEST-ECLI-NSS-GREEN',
      rawText: 'rozsudek NSS, č. j. TEST-3 Ads 3/2003',
      light: 'GREEN',
      reasons: [],
    },
  ],
  unresolved: [
    { rawText: 'citovaného rozhodnutí', reason: 'not resolved' },
    { rawText: 'usnesení NS, sp. zn. TEST-4 Cdo 4/2004', reason: 'not in corpus' },
  ],
};

export const MOCK_DOCUMENT_TEXT =
  'Ukázkový text. Krajský soud odkázal na rozsudek NSS ze dne 12. 3. 2015, ' +
  'č. j. 6 Ads 45/2014-32, a dále na § 2000 odst. 1 zákona č. 89/2012 Sb. ' +
  'Ze závěrů citovaného rozhodnutí vychází i napadené rozhodnutí.';

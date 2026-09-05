/**
 * All user-facing wording lives here.
 *
 * Czech law has no doctrine of binding precedent, so the vocabulary is the product
 * (CLAUDE.md "Language and terminology", PLAN.md section 16). Never translate a
 * common-law term. Never say that a source is "platný": the assertion is always scoped
 * to a corpus and a date (PLAN.md section 2).
 */

import type { CorpusCoverage, Light, Reason, ReasonKind } from './types';

/** Colour name, for the screen reader and the tooltip. */
export const LIGHT_NAME: Record<Light, string> = {
  GREEN: 'zelená',
  AMBER: 'oranžová',
  RED: 'červená',
};

export const NO_ADVERSE_TREATMENT = 'nenalezeno žádné nepříznivé nakládání';

/**
 * The headline word for one reason. Red is *překonáno* or *zrušeno*, amber is
 * *argumentačně oslabeno* or *zúženo*, depending on which reason drove the light.
 * `UNCLASSIFIED` is the one amber that gets neither: PLAN.md section 9 marks it
 * "needs review", and calling a failed classification *argumentačně oslabeno* would
 * assert something the evidence does not support.
 */
const REASON_HEADLINE: Record<string, string> = {
  QUASHED: 'zrušeno',
  SUPERSEDED: 'překonáno',
  PROVISION_DEROGATED: 'zrušeno ustanovení',
  CONFLICT: 'argumentačně oslabeno',
  NARROWED: 'zúženo',
  PROVISION_REWORDED: 'argumentačně oslabeno',
  INHERITED_WEAKNESS: 'argumentačně oslabeno',
  UNCLASSIFIED: 'vyžaduje ruční kontrolu',
};

/** One sentence explaining what the reason means, without repeating the identifiers. */
const REASON_DETAIL: Record<string, string> = {
  QUASHED: 'Citující rozhodnutí tento zdroj svým výrokem zrušilo.',
  SUPERSEDED:
    'Citující rozhodnutí se od dosavadního výkladu odchýlilo a podle tabulky pravomocí tak učinit mohlo.',
  PROVISION_DEROGATED: 'Ustanovení, o které se zdroj opírá, bylo zrušeno Ústavním soudem.',
  CONFLICT:
    'Citující rozhodnutí vyslovilo odlišný právní názor, nemá však pravomoc dosavadní výklad změnit.',
  NARROWED: 'Citující rozhodnutí závěr přijalo, ale zúžilo okruh případů, na které dopadá.',
  PROVISION_REWORDED:
    'Ustanovení, o které se zdroj opírá, bylo po jeho vydání přeformulováno. Obě znění si porovnejte sami.',
  INHERITED_WEAKNESS:
    'Zdroj se opírá o jiný zdroj, u něhož bylo nalezeno nepříznivé nakládání. Přenos se sleduje pouze o jeden krok.',
  UNCLASSIFIED:
    'Povahu vztahu se nepodařilo spolehlivě určit. Vyžaduje ruční kontrolu člověkem.',
};

/** Fallback headline when the API sends a reason kind this build does not know. */
const UNKNOWN_HEADLINE: Record<Light, string> = {
  GREEN: NO_ADVERSE_TREATMENT,
  AMBER: 'argumentačně oslabeno',
  RED: 'překonáno',
};

/**
 * Evaluation order of PLAN.md section 9, first match wins. Used only to pick which reason
 * supplies the headline word; the light itself is decided by StatusEngine, never here.
 */
const REASON_PRECEDENCE: readonly string[] = [
  'QUASHED',
  'SUPERSEDED',
  'PROVISION_DEROGATED',
  'CONFLICT',
  'NARROWED',
  'PROVISION_REWORDED',
  'UNCLASSIFIED',
  'INHERITED_WEAKNESS',
];

export function leadingReason(reasons: readonly Reason[]): Reason | undefined {
  for (const kind of REASON_PRECEDENCE) {
    const hit = reasons.find((r) => r.kind === kind);
    if (hit) return hit;
  }
  return reasons[0];
}

/** The phrase shown next to the traffic light on a source row. */
export function verdictPhrase(light: Light, reasons: readonly Reason[]): string {
  if (light === 'GREEN' || reasons.length === 0) return NO_ADVERSE_TREATMENT;
  const lead = leadingReason(reasons);
  const headline = lead ? REASON_HEADLINE[lead.kind] : undefined;
  return headline ?? UNKNOWN_HEADLINE[light];
}

export function reasonHeadline(kind: ReasonKind, light: Light): string {
  return REASON_HEADLINE[kind] ?? UNKNOWN_HEADLINE[light];
}

export function reasonDetail(kind: ReasonKind): string {
  return REASON_DETAIL[kind] ?? 'Nepříznivé nakládání zaznamenané v korpusu.';
}

// --- proposition check (M7) --------------------------------------------------

/**
 * The four verdicts of PLAN.md section 12, in Czech.
 *
 * Deliberately a different register from the traffic-light vocabulary above. *Překonáno*
 * and *zúženo* describe what later case law did to the source; these describe the relation
 * between the *user's own sentence* and that source. A bare participle would blur the two,
 * and "your claim is too broad" being mistaken for "this decision was narrowed" is the one
 * confusion this feature cannot afford — they point at different documents.
 *
 * `UNCLASSIFIED` is not a verdict. It is the model failing the evidence-span gate twice,
 * and it says so rather than dressing a failure as an answer (CLAUDE.md rule 3).
 */
const PROPOSITION_VERDICT: Record<string, string> = {
  SUPPORTS: 'tvrzení odpovídá zdroji',
  OVERBROAD: 'tvrzení jde nad rámec zdroje',
  UNRELATED: 'zdroj se k tvrzení nevyjadřuje',
  CONTRADICTS: 'zdroj tvrdí opak',
  UNCLASSIFIED: 'nepodařilo se posoudit',
};

/** How strongly the verdict reads on the page. Only OVERBROAD/CONTRADICTS are adverse. */
export type PropositionTone = 'ok' | 'warn' | 'bad' | 'unknown';

const PROPOSITION_TONE: Record<string, PropositionTone> = {
  SUPPORTS: 'ok',
  OVERBROAD: 'warn',
  UNRELATED: 'warn',
  CONTRADICTS: 'bad',
  UNCLASSIFIED: 'unknown',
};

export function propositionVerdict(verdict: string): string {
  return PROPOSITION_VERDICT[verdict] ?? verdict;
}

export function propositionTone(verdict: string): PropositionTone {
  return PROPOSITION_TONE[verdict] ?? 'unknown';
}

/**
 * The scope sentence for a proposition check. It is a statement about one decision's own
 * text, not about the corpus, so it must not borrow the corpus caveat — this check would
 * give the same answer against a corpus of one.
 */
export const PROPOSITION_CAVEAT =
  'Posouzeno pouze proti textu tohoto rozhodnutí, nikoli proti pozdější judikatuře. ' +
  'Zda zdroj stále obstojí, říká semafor výše; toto je odpověď na jinou otázku.';

export const PROPOSITION_HELP =
  'Napište tvrzení, pro které tento zdroj ve svém dokumentu uvádíte — jednou větou, ' +
  'tak jak by stálo v podání.';

const PANEL_NAME: Record<string, string> = {
  panel: 'senát',
  extended: 'rozšířený senát',
  grand: 'velký senát',
  plenary: 'plénum',
  unknown: 'druh senátu neuveden',
};

export function panelName(panel: string | null | undefined): string | null {
  if (!panel) return null;
  return PANEL_NAME[panel] ?? panel;
}

const UNRESOLVED_REASON: Record<string, string> = {
  'not in corpus': 'není v korpusu',
  'not resolved': 'odkaz se nepodařilo přiřadit',
  ambiguous: 'odkaz odpovídá více rozhodnutím',
};

export function unresolvedReason(reason: string): string {
  return UNRESOLVED_REASON[reason] ?? reason;
}

// --- formatting -------------------------------------------------------------

const DATE_FORMAT = new Intl.DateTimeFormat('cs-CZ', {
  day: 'numeric',
  month: 'numeric',
  year: 'numeric',
});

/** ISO date to `1. 9. 2026`. Returns the input unchanged if it is not a date. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return DATE_FORMAT.format(parsed);
}

export function formatCount(n: number): string {
  return n.toLocaleString('cs-CZ');
}

export type CorpusScope = {
  total: number;
  through: string | null;
  courts: string[];
};

/** Collapse the corpus block into the numbers the scope sentence needs. */
export function corpusScope(corpus: Record<string, CorpusCoverage>): CorpusScope {
  const courts = Object.keys(corpus).sort();
  let total = 0;
  let through: string | null = null;
  for (const court of courts) {
    const coverage = corpus[court];
    if (!coverage) continue;
    total += coverage.count;
    // Coverage of the whole corpus reaches only as far as its least current court.
    if (through === null || coverage.through < through) through = coverage.through;
  }
  return { total, through, courts };
}

/**
 * The scope sentence of PLAN.md section 2, in the page header. Deliberately never the
 * word "platný".
 */
export function scopeSentence(asOf: string, scope: CorpusScope): string {
  const courts = scope.courts.length > 0 ? scope.courts.join(', ') : 'zatím žádný soud';
  return (
    `Posouzeno ke dni ${formatDate(asOf)} proti korpusu ${formatCount(scope.total)} rozhodnutí ` +
    `(${courts}) zveřejněných do ${formatDate(scope.through)}.`
  );
}

export const SCOPE_CAVEAT =
  'Výsledek se vztahuje pouze k tomuto korpusu a k tomuto dni. Říká, zda v něm bylo ' +
  'nalezeno nepříznivé nakládání s citovaným zdrojem — neříká, že zdroj je bez vady. ' +
  'Ne každé rozhodnutí je zveřejněno, korpus má proto mezery.';

/** The per-source restatement of the same scope, shown when a row is expanded. */
export function sourceScopeSentence(asOf: string, scope: CorpusScope, phrase: string): string {
  return (
    `Pro tento zdroj tak, jak je použit ve vašem dokumentu, posouzeno ke dni ` +
    `${formatDate(asOf)} proti korpusu ${formatCount(scope.total)} rozhodnutí zveřejněných do ` +
    `${formatDate(scope.through)}: ${phrase}.`
  );
}

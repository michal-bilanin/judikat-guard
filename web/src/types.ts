/**
 * Wire types. These mirror `DocumentReport` in PLAN.md section 11 and the `Reason`
 * variants of `StatusEngine` in PLAN.md section 9. The Java side is written against the
 * same spec; keep this file in step with it and change nothing here unilaterally.
 */

export type Light = 'GREEN' | 'AMBER' | 'RED';

/**
 * One variant per `Reason` record in PLAN.md section 9, plus `UNCLASSIFIED` for the
 * "model failed validation twice, needs review" amber. The `(string & {})` arm keeps an
 * unknown kind from breaking the page: it renders with a neutral fallback phrase instead.
 */
export type ReasonKind =
  | 'QUASHED'
  | 'SUPERSEDED'
  | 'CONFLICT'
  | 'NARROWED'
  | 'PROVISION_REWORDED'
  | 'PROVISION_DEROGATED'
  | 'INHERITED_WEAKNESS'
  | 'UNCLASSIFIED'
  | (string & {});

/**
 * The union of the fields carried by the `Reason` variants, flattened onto one object as
 * in the PLAN section 11 example (`{ kind, byEcli, span, paragraph }`). Everything except
 * `kind` is optional because which fields are present depends on the variant.
 */
export type Reason = {
  kind: ReasonKind;
  /** Citing decision (Quashed, Superseded, Conflict, Narrowed, ProvisionDerogated). */
  byEcli?: string | null;
  /** Verbatim evidence span. The thing the user actually checks. */
  span?: string | null;
  /** Paragraph index in the citing decision the span was taken from. */
  paragraph?: number | null;
  /** panel | extended | grand | plenary | unknown (Superseded). */
  panel?: string | null;
  /** InheritedWeakness: the source this one relies on. */
  viaEcli?: string | null;
  /** Provision reasons. */
  provisionId?: number | null;
  changedOn?: string | null;
  material?: boolean | null;
};

/** One entry of the `corpus` block: how much of one court we have, and through when. */
export type CorpusCoverage = {
  count: number;
  through: string;
};

export type SourceReport = {
  /** Null for a statutory provision, which has no ECLI. */
  ecli?: string | null;
  /** The citation exactly as it appeared in the uploaded document. Never reconstructed. */
  rawText: string;
  light: Light;
  reasons: Reason[];
};

export type UnresolvedRef = {
  rawText: string;
  reason: string;
};

export type DocumentReport = {
  asOf: string;
  corpus: Record<string, CorpusCoverage>;
  sources: SourceReport[];
  unresolved: UnresolvedRef[];
};

export type CheckRequest = {
  text: string;
};

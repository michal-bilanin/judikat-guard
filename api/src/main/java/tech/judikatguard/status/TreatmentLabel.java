package tech.judikatguard.status;

/**
 * How a citing decision treats the decision it cites. PLAN.md section 8.
 *
 * <p>Identifiers stay English; user-facing Czech wording is applied in the web layer
 * (<em>překonáno</em>, <em>argumentačně oslabeno</em>, <em>zrušeno</em>, <em>zúženo</em>).
 * Do not add an eighth substantive label without editing PLAN.md.
 */
public enum TreatmentLabel {
    /** Applies the cited rule and agrees with it. */
    FOLLOWED,
    /** Background reference, no reliance. */
    MENTIONED,
    /** Accepts the rule, holds the facts differ. */
    DISTINGUISHED,
    /** Accepts the rule but limits where it applies. */
    NARROWED,
    /** Replaces the cited interpretation. */
    DEPARTED,
    /** Disagrees without authority to change anything. */
    CRITICIZED,
    /** Formally annuls the cited decision. */
    QUASHED,
    /** Model output failed evidence-span validation twice. Surfaced as amber "needs review". */
    UNCLASSIFIED
}

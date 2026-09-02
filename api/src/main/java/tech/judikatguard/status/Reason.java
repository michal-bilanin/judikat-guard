package tech.judikatguard.status;

import java.time.LocalDate;
import java.util.Objects;

/**
 * A single traceable fact behind a light. Closed set: the evidence panel renders one Czech
 * phrasing per variant, so adding a variant is a deliberate product decision.
 *
 * <p>Every variant carries enough to locate the underlying row: an ECLI, a span, or a
 * provision id. A light other than GREEN always has at least one of these (hard rule 2).
 */
public sealed interface Reason {

    /** Formally annulled by {@code byEcli}. Rendered <em>zrušeno</em>. */
    record Quashed(String byEcli, String span) implements Reason {
        public Quashed {
            Objects.requireNonNull(byEcli, "byEcli");
            Objects.requireNonNull(span, "span");
        }
    }

    /**
     * Departed from by a body that had the authority to do so. Rendered <em>překonáno</em>.
     *
     * @param panel the citing panel type that carried the authority, e.g. {@code extended}
     */
    record Superseded(String byEcli, String panel, String span) implements Reason {
        public Superseded {
            Objects.requireNonNull(byEcli, "byEcli");
            Objects.requireNonNull(panel, "panel");
            Objects.requireNonNull(span, "span");
        }
    }

    /**
     * Departed from by a body that could not bind: a conflict in the case law rather than a
     * supersession. Rendered <em>argumentačně oslabeno</em>.
     */
    record Conflict(String byEcli, String span) implements Reason {
        public Conflict {
            Objects.requireNonNull(byEcli, "byEcli");
            Objects.requireNonNull(span, "span");
        }
    }

    /** Scope limited by a later decision. Rendered <em>zúženo</em>. */
    record Narrowed(String byEcli, String span) implements Reason {
        public Narrowed {
            Objects.requireNonNull(byEcli, "byEcli");
            Objects.requireNonNull(span, "span");
        }
    }

    /**
     * A provision relied on was reworded after the decision was handed down, and the change
     * was judged to touch the reasoning. PLAN.md section 10.
     */
    record ProvisionReworded(long provisionId, LocalDate changedOn, boolean material)
            implements Reason {
        public ProvisionReworded {
            Objects.requireNonNull(changedOn, "changedOn");
        }
    }

    /** A provision relied on was struck down by the Constitutional Court. */
    record ProvisionDerogated(long provisionId, String byEcli) implements Reason {
        public ProvisionDerogated {
            Objects.requireNonNull(byEcli, "byEcli");
        }
    }

    /**
     * A citation whose classification failed evidence-span validation twice. Not an adverse
     * finding, a coverage gap: the user is told to look at it themselves.
     */
    record NeedsReview(String byEcli, long citationId) implements Reason {
        public NeedsReview {
            Objects.requireNonNull(byEcli, "byEcli");
        }
    }

    /** A source this decision wholly relies on is itself red. One hop only. */
    record InheritedWeakness(String viaEcli) implements Reason {
        public InheritedWeakness {
            Objects.requireNonNull(viaEcli, "viaEcli");
        }
    }
}

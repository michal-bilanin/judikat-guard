package tech.judikatguard.llm;

import java.util.Objects;
import java.util.Set;
import java.util.TreeSet;

/**
 * The answer to "are you using this decision correctly". PLAN.md section 12.
 *
 * <p>Not a validity check: this one asks whether the claim the document offers the decision
 * for is actually what the decision held. The demo line is <em>"you cite this decision for
 * X, but it held X only for movable property"</em>.
 *
 * @param verdict one of {@link #SUPPORTS}, {@link #OVERBROAD}, {@link #UNRELATED},
 *     {@link #CONTRADICTS}, or {@link #UNCLASSIFIED} when the model failed the evidence-span
 *     gate twice
 * @param confidence the model's own probability, in {@code [0, 1]}; 0 for
 *     {@link #UNCLASSIFIED}, whose confidence is meaningless by construction
 * @param evidenceSpan a verbatim quote of the holding or the passages the model was shown,
 *     already validated by {@link EvidenceSpan}; empty only for {@link #UNCLASSIFIED}
 * @param note one sentence shown to the user, so written in Czech by the prompt. For
 *     {@link #OVERBROAD} it names the limit the decision imposed
 */
public record PropositionVerdict(
        String verdict, double confidence, String evidenceSpan, String note) {

    /** The decision holds what the claim says it holds. */
    public static final String SUPPORTS = "SUPPORTS";

    /** The decision supports a narrower version of the claim. The interesting case. */
    public static final String OVERBROAD = "OVERBROAD";

    /** The decision does not address the claim either way. */
    public static final String UNRELATED = "UNRELATED";

    /** The decision holds the opposite of the claim. */
    public static final String CONTRADICTS = "CONTRADICTS";

    /**
     * The model's reply failed validation twice. Mirrors
     * {@code TreatmentLabel.UNCLASSIFIED}: surfaced as amber "needs review", never as an
     * answer (CLAUDE.md rule 3).
     */
    public static final String UNCLASSIFIED = "UNCLASSIFIED";

    /** The four verdicts the prompt may return. */
    public static final Set<String> VERDICTS = Set.of(SUPPORTS, OVERBROAD, UNRELATED, CONTRADICTS);

    public PropositionVerdict {
        Objects.requireNonNull(verdict, "verdict");
        Objects.requireNonNull(evidenceSpan, "evidenceSpan");
        Objects.requireNonNull(note, "note");
        if (!VERDICTS.contains(verdict) && !UNCLASSIFIED.equals(verdict)) {
            throw new IllegalArgumentException(
                    "verdict must be one of " + new TreeSet<>(VERDICTS) + " or " + UNCLASSIFIED
                            + ", got " + verdict);
        }
        if (!Double.isFinite(confidence) || confidence < 0.0 || confidence > 1.0) {
            throw new IllegalArgumentException("confidence must be in [0, 1], got " + confidence);
        }
        if (UNCLASSIFIED.equals(verdict)) {
            if (!evidenceSpan.isBlank() || confidence != 0.0) {
                throw new IllegalArgumentException(
                        "UNCLASSIFIED carries no evidence and no confidence; use unclassified(note)");
            }
        } else if (evidenceSpan.isBlank()) {
            // CLAUDE.md rule 2, applied one layer earlier than the traffic light: a verdict
            // the evidence panel cannot quote must not exist as a value in the first place.
            throw new IllegalArgumentException("no verdict without evidence: " + verdict);
        }
    }

    /**
     * The failure state. Written when the model's reply could not be validated twice running,
     * which the UI shows as needing review rather than as an answer.
     *
     * @param note Czech, shown to the user
     */
    public static PropositionVerdict unclassified(String note) {
        return new PropositionVerdict(UNCLASSIFIED, 0.0, "", note);
    }

    public boolean isUnclassified() {
        return UNCLASSIFIED.equals(verdict);
    }
}

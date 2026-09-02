package tech.judikatguard.status;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.Objects;
import org.jspecify.annotations.Nullable;

/**
 * One classified citation pointing at the decision under evaluation: a {@code treatment}
 * row joined to its {@code citation} and to the citing {@code decision}.
 *
 * @param citationId      the {@code citation.id} this treatment was derived from
 * @param citingEcli      the decision that did the treating
 * @param citingCourt     {@code US} | {@code NSS} | {@code NS}
 * @param citingPanel     {@code panel} | {@code extended} | {@code grand} | {@code plenary} | {@code unknown}
 * @param citingDate      {@code decision.decided_on} of the citing decision; compared against {@code asOf}
 * @param label           the classified treatment
 * @param confidence      {@code numeric(3,2)}; compared against {@link Thresholds}
 * @param evidenceSpan    verbatim substring of the classified context, shown in the evidence panel
 * @param paragraphIdx    paragraph the citation sits in; null when the extractor could not place it
 * @param citingMayDepart resolved from the {@code departure_authority} table (PLAN.md section 6),
 *                        never decided in code
 */
public record EvidenceRow(
        long citationId,
        String citingEcli,
        String citingCourt,
        String citingPanel,
        LocalDate citingDate,
        TreatmentLabel label,
        BigDecimal confidence,
        String evidenceSpan,
        @Nullable Integer paragraphIdx,
        boolean citingMayDepart) {

    public EvidenceRow {
        Objects.requireNonNull(citingEcli, "citingEcli");
        Objects.requireNonNull(citingCourt, "citingCourt");
        Objects.requireNonNull(citingPanel, "citingPanel");
        Objects.requireNonNull(citingDate, "citingDate");
        Objects.requireNonNull(label, "label");
        Objects.requireNonNull(confidence, "confidence");
        Objects.requireNonNull(evidenceSpan, "evidenceSpan");
    }
}

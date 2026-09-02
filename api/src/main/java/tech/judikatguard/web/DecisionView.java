package tech.judikatguard.web;

import com.fasterxml.jackson.annotation.JsonInclude;
import java.time.LocalDate;
import java.util.Objects;
import org.jspecify.annotations.Nullable;
import tech.judikatguard.decision.DecisionSummary;

/**
 * {@code GET /api/decisions/{ecli}}: metadata plus the <em>právní věta</em>.
 *
 * <p>{@code journalNo} present means the decision was published in the official reporter,
 * which signals higher authority. Its absence is normal, as is the absence of a headnote:
 * neither is an error and neither is guessed at.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record DecisionView(
        String ecli,
        String court,
        String panelType,
        LocalDate decidedOn,
        @Nullable String caseNo,
        @Nullable String refNo,
        @Nullable String journalNo,
        @Nullable String ratioSummary,
        String sourceUrl) {

    public DecisionView {
        Objects.requireNonNull(ecli, "ecli");
        Objects.requireNonNull(court, "court");
        Objects.requireNonNull(panelType, "panelType");
        Objects.requireNonNull(decidedOn, "decidedOn");
        Objects.requireNonNull(sourceUrl, "sourceUrl");
    }

    public static DecisionView of(DecisionSummary summary) {
        return new DecisionView(
                summary.ecli(),
                summary.courtCode(),
                summary.panelType(),
                summary.decidedOn(),
                summary.caseNo(),
                summary.refNo(),
                summary.journalNo(),
                summary.ratioSummary(),
                summary.sourceUrl());
    }
}

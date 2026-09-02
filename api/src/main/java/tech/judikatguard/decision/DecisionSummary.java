package tech.judikatguard.decision;

import java.time.LocalDate;
import java.util.Objects;
import org.jspecify.annotations.Nullable;

/**
 * One row of {@code decision}, minus the embedding. What {@code GET /api/decisions/{ecli}}
 * answers with, and the source of {@code decided_on} for the provision comparison.
 *
 * <p>{@code caseNo}, {@code refNo}, {@code journalNo} and {@code ratioSummary} are all
 * normal absences rather than errors: not every decision is published in the reporter and
 * not every decision carries a <em>právní věta</em> (PLAN.md section 4, JSpecify note).
 *
 * @param panelType {@code panel} | {@code extended} | {@code grand} | {@code plenary} |
 *     {@code unknown}; the input to the {@code departure_authority} join
 */
public record DecisionSummary(
        String ecli,
        String courtCode,
        String panelType,
        LocalDate decidedOn,
        @Nullable String caseNo,
        @Nullable String refNo,
        @Nullable String journalNo,
        @Nullable String ratioSummary,
        String sourceUrl) {

    public DecisionSummary {
        Objects.requireNonNull(ecli, "ecli");
        Objects.requireNonNull(courtCode, "courtCode");
        Objects.requireNonNull(panelType, "panelType");
        Objects.requireNonNull(decidedOn, "decidedOn");
        Objects.requireNonNull(sourceUrl, "sourceUrl");
    }

    /** The <em>právní věta</em> or an empty string. The prompt contract accepts empty. */
    public String ratioOrEmpty() {
        return ratioSummary == null ? "" : ratioSummary;
    }
}

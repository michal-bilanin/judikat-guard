package tech.judikatguard.document;

import java.util.List;
import java.util.Objects;
import org.jspecify.annotations.Nullable;
import tech.judikatguard.status.Light;

/**
 * One source the uploaded document relies on. The shape is PLAN.md section 11 and
 * {@code SourceReport} in {@code web/src/types.ts}.
 *
 * @param ecli null for a statutory provision, which has no ECLI
 * @param rawText the citation exactly as it appeared in the document, never reconstructed
 *     from the resolved identifiers. The user has to be able to find it on their own page
 * @param reasons every applicable fact in evaluation order; empty exactly when GREEN, which
 *     the UI renders as "nenalezeno žádné nepříznivé nakládání"
 */
public record SourceReport(
        @Nullable String ecli, String rawText, Light light, List<ReasonView> reasons) {

    public SourceReport {
        Objects.requireNonNull(rawText, "rawText");
        Objects.requireNonNull(light, "light");
        reasons = List.copyOf(reasons);
    }
}

package tech.judikatguard.status;

import java.time.LocalDate;
import java.util.List;
import java.util.Objects;

/**
 * The verdict for one source, always scoped: as of {@code asOf}, against {@code corpusSize}
 * decisions published through {@code corpusThrough}. PLAN.md section 2.
 *
 * <p>{@code reasons} is every applicable fact in evaluation order, not just the ones that
 * decided the light. Empty exactly when the light is GREEN.
 */
public record Status(
        String ecli,
        Light light,
        LocalDate asOf,
        int corpusSize,
        LocalDate corpusThrough,
        List<Reason> reasons) {

    public Status {
        Objects.requireNonNull(ecli, "ecli");
        Objects.requireNonNull(light, "light");
        Objects.requireNonNull(asOf, "asOf");
        Objects.requireNonNull(corpusThrough, "corpusThrough");
        reasons = List.copyOf(reasons);
    }
}

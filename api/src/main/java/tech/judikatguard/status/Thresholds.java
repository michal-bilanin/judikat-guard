package tech.judikatguard.status;

import java.math.BigDecimal;
import java.util.Objects;

/**
 * Minimum classifier confidence for a treatment row to count as evidence.
 *
 * <p>D8, asymmetric on purpose. A false red destroys trust permanently; a false amber costs
 * the user thirty seconds. Hence a high bar for red and a lower one for amber, accepting
 * lower recall on red. Say so in the presentation rather than hiding it.
 *
 * <p>{@code UNCLASSIFIED} is exempt from both bars: its confidence is meaningless by
 * construction, since the row exists precisely because validation failed.
 */
public record Thresholds(BigDecimal redMinConfidence, BigDecimal amberMinConfidence) {

    private static final Thresholds DEFAULTS =
            new Thresholds(new BigDecimal("0.85"), new BigDecimal("0.60"));

    public Thresholds {
        Objects.requireNonNull(redMinConfidence, "redMinConfidence");
        Objects.requireNonNull(amberMinConfidence, "amberMinConfidence");
    }

    public static Thresholds defaults() {
        return DEFAULTS;
    }
}

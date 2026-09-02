package tech.judikatguard.decision;

import java.time.LocalDate;
import java.util.Objects;

/**
 * How much of one court this system has, and through when. One row of {@code corpus_meta}.
 *
 * <p>The field names are the wire names of the {@code corpus} block in PLAN.md section 11
 * ({@code { "NSS": { "count": 3142, "through": "2026-08-15" } }}) and of
 * {@code web/src/types.ts}.
 */
public record CorpusCoverage(int count, LocalDate through) {

    public CorpusCoverage {
        Objects.requireNonNull(through, "through");
    }
}

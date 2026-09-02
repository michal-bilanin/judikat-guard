package tech.judikatguard.decision;

import java.time.LocalDate;
import java.util.Collections;
import java.util.Map;
import java.util.Objects;
import java.util.SortedMap;
import java.util.TreeMap;

/**
 * The scope every verdict is stated against: how many decisions, published through when,
 * per court. PLAN.md section 2.
 *
 * <p>Coverage of the whole corpus reaches only as far as its <em>least</em> current court,
 * so {@link #through(LocalDate)} takes the minimum rather than the maximum. Overstating
 * coverage is the one error that turns a scoped answer into a false claim.
 */
public record Corpus(SortedMap<String, CorpusCoverage> courts) {

    public Corpus {
        courts = Collections.unmodifiableSortedMap(
                new TreeMap<>(Objects.requireNonNull(courts, "courts")));
    }

    public static Corpus of(Map<String, CorpusCoverage> courts) {
        return new Corpus(new TreeMap<>(courts));
    }

    /** Total decisions the answer was computed against. */
    public int size() {
        return courts.values().stream().mapToInt(CorpusCoverage::count).sum();
    }

    /**
     * The publication date the corpus is covered through: the earliest per-court date.
     *
     * @param emptyFallback used when {@code corpus_meta} has no rows at all, which is the
     *     real state of the system before the first crawl finishes. The pairing with
     *     {@link #size()} of 0 keeps the sentence honest: "0 decisions published through
     *     &lt;the date you asked about&gt;" claims no coverage.
     */
    public LocalDate through(LocalDate emptyFallback) {
        return courts.values().stream()
                .map(CorpusCoverage::through)
                .min(LocalDate::compareTo)
                .orElse(emptyFallback);
    }
}

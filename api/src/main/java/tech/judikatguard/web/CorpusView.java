package tech.judikatguard.web;

import java.time.LocalDate;
import java.util.Map;
import java.util.Objects;
import tech.judikatguard.decision.Corpus;
import tech.judikatguard.decision.CorpusCoverage;

/**
 * {@code GET /api/corpus}: how much of each court this system has, and through when.
 *
 * <p>{@code through} is the earliest per-court date, not the latest. Coverage of the whole
 * corpus reaches only as far as its least current court, and overstating it is the one
 * error that turns a scoped answer into a false claim.
 */
public record CorpusView(int size, LocalDate through, Map<String, CorpusCoverage> courts) {

    public CorpusView {
        Objects.requireNonNull(through, "through");
        courts = Map.copyOf(courts);
    }

    /**
     * @param emptyFallback the date reported when {@code corpus_meta} has no rows, which is
     *     the real state before the first crawl finishes. Paired with a size of 0 it claims
     *     no coverage at all.
     */
    public static CorpusView of(Corpus corpus, LocalDate emptyFallback) {
        return new CorpusView(corpus.size(), corpus.through(emptyFallback), corpus.courts());
    }
}

package tech.judikatguard.status;

import java.time.LocalDate;
import java.util.List;
import java.util.Objects;

/**
 * Everything {@link StatusEngine} is allowed to know. One value in, one {@link Status} out.
 *
 * <p>The engine performs no lookups, so the caller has already done the joins: treatment
 * rows with their departure authority resolved, provisions with their version comparison
 * resolved, and the one-hop inherited lights computed. Corpus figures come from
 * {@code corpus_meta} and exist so the verdict can be stated with its scope attached.
 *
 * @param ecli          the source being evaluated
 * @param asOf          the date the question is asked about; evidence dated later is ignored
 * @param corpusSize    number of decisions the answer was computed against
 * @param corpusThrough publication coverage date of that corpus
 */
public record Evaluation(
        String ecli,
        LocalDate asOf,
        int corpusSize,
        LocalDate corpusThrough,
        List<EvidenceRow> treatments,
        List<ProvisionEvidence> provisions,
        List<InheritedEvidence> inherited,
        Thresholds thresholds) {

    public Evaluation {
        Objects.requireNonNull(ecli, "ecli");
        Objects.requireNonNull(asOf, "asOf");
        Objects.requireNonNull(corpusThrough, "corpusThrough");
        Objects.requireNonNull(thresholds, "thresholds");
        // Defensive copies: the engine is a pure function of this value, so the value has to
        // stop being able to change underneath a caller that kept a reference to the lists.
        treatments = List.copyOf(treatments);
        provisions = List.copyOf(provisions);
        inherited = List.copyOf(inherited);
    }
}

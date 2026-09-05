package tech.judikatguard.web;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import tech.judikatguard.document.ReasonView;
import tech.judikatguard.document.StatusService.Evaluated;
import tech.judikatguard.status.Light;
import tech.judikatguard.status.Status;

/**
 * {@code GET /api/decisions/{ecli}/status}: a traffic light plus the evidence behind it.
 *
 * <p>{@code corpusSize}, {@code corpusThrough} and {@code verdict} are not decoration. A
 * light on its own would read as a claim about the source; with them it reads as what it
 * actually is, a statement about one date and one corpus (PLAN.md section 2).
 *
 * @param verdict the Czech scope sentence. GREEN carries the "no adverse treatment found"
 *     phrasing that CLAUDE.md rule 2 requires, never the word <em>platný</em>
 * @param links the same ECLI-to-court-page table as {@code DocumentReport}, so a client
 *     reading one decision can open the evidence for itself without a second round trip
 */
public record StatusView(
        String ecli,
        Light light,
        LocalDate asOf,
        int corpusSize,
        LocalDate corpusThrough,
        String verdict,
        List<ReasonView> reasons,
        Map<String, String> links) {

    public StatusView {
        Objects.requireNonNull(ecli, "ecli");
        Objects.requireNonNull(light, "light");
        Objects.requireNonNull(asOf, "asOf");
        Objects.requireNonNull(corpusThrough, "corpusThrough");
        Objects.requireNonNull(verdict, "verdict");
        reasons = List.copyOf(reasons);
        links = Map.copyOf(links);
    }

    /**
     * @param links resolved by the controller, which owns the repository; this record stays
     *     a pure projection of an {@link Evaluated}
     */
    public static StatusView of(Evaluated evaluated, Map<String, String> links) {
        Status status = evaluated.status();
        List<ReasonView> reasons = evaluated.reasons();
        return new StatusView(
                status.ecli(),
                status.light(),
                status.asOf(),
                status.corpusSize(),
                status.corpusThrough(),
                Wording.scopedVerdict(
                        status.asOf(),
                        status.corpusSize(),
                        status.corpusThrough(),
                        Wording.phrase(status.light(), reasons)),
                reasons,
                links);
    }
}

package tech.judikatguard.document;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import tech.judikatguard.decision.CorpusCoverage;

/**
 * The answer to {@code POST /api/documents/check}. The JSON of PLAN.md section 11, field
 * for field, and the wire contract of {@code DocumentReport} in {@code web/src/types.ts}.
 *
 * <pre>{@code
 * {
 *   "asOf": "2026-09-01",
 *   "corpus": { "NSS": { "count": 3142, "through": "2026-08-15" } },
 *   "sources": [ { "ecli": "...", "rawText": "...", "light": "AMBER", "reasons": [...] } ],
 *   "unresolved": [ { "rawText": "...", "reason": "not in corpus" } ]
 * }
 * }</pre>
 *
 * <p>{@code asOf} and {@code corpus} are not decoration: they are the scope the whole report
 * is stated under. Without them the list of lights would read as a claim that the document
 * is sound, which is not something this system can know (PLAN.md section 2).
 *
 * <p>{@code unresolved} is always present, empty list included. Never an error, never
 * omitted.
 */
public record DocumentReport(
        LocalDate asOf,
        Map<String, CorpusCoverage> corpus,
        List<SourceReport> sources,
        List<UnresolvedReference> unresolved) {

    public DocumentReport {
        Objects.requireNonNull(asOf, "asOf");
        corpus = Map.copyOf(corpus);
        sources = List.copyOf(sources);
        unresolved = List.copyOf(unresolved);
    }
}

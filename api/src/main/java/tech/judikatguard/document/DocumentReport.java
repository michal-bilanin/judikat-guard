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
 *   "links": { "ECLI:CZ:NSS:2024:2.As.103.2023.47": "https://vyhledavac.nssoud.cz/..." },
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
 *
 * @param links every decision named anywhere in the report — as a source, as a citing
 *     decision, or as a link in a reliance chain — mapped to the court page it was crawled
 *     from. One table rather than a URL per reason, because a citing decision that produced
 *     three reasons is still one decision. An ECLI outside the corpus is absent, and the UI
 *     renders it as plain text; a missing link is never allowed to look like a missing
 *     verdict
 */
public record DocumentReport(
        LocalDate asOf,
        Map<String, CorpusCoverage> corpus,
        List<SourceReport> sources,
        Map<String, String> links,
        List<UnresolvedReference> unresolved) {

    public DocumentReport {
        Objects.requireNonNull(asOf, "asOf");
        corpus = Map.copyOf(corpus);
        sources = List.copyOf(sources);
        links = Map.copyOf(links);
        unresolved = List.copyOf(unresolved);
    }
}

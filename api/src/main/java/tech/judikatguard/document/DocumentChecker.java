package tech.judikatguard.document;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import org.springframework.stereotype.Service;
import tech.judikatguard.decision.ProvisionRepository;
import tech.judikatguard.document.StatusService.Evaluated;
import tech.judikatguard.extract.CitationExtractor;
import tech.judikatguard.extract.Reference;
import tech.judikatguard.status.Light;
import tech.judikatguard.status.ProvisionEvidence;

/**
 * {@code POST /api/documents/check}: extract, resolve, read the graph, report. PLAN.md
 * section 11.
 *
 * <p>The only new work a check does is extraction. Everything else is a read of a graph the
 * pipeline has already classified — no batch classification at request time, and no model
 * call on this path at all.
 *
 * <p><b>Nothing is written.</b> The uploaded text is not stored and no {@code citation} row
 * is created for it: {@code citation.citing_ecli} is {@code not null} because that table
 * describes the crawled corpus, and a document somebody pasted in has no place in it.
 */
@Service
public final class DocumentChecker {

    private final CitationExtractor extractor;
    private final ReferenceResolver resolver;
    private final StatusService statuses;
    private final ProvisionRepository provisions;

    public DocumentChecker(
            CitationExtractor extractor,
            ReferenceResolver resolver,
            StatusService statuses,
            ProvisionRepository provisions) {
        this.extractor = Objects.requireNonNull(extractor, "extractor");
        this.resolver = Objects.requireNonNull(resolver, "resolver");
        this.statuses = Objects.requireNonNull(statuses, "statuses");
        this.provisions = Objects.requireNonNull(provisions, "provisions");
    }

    public DocumentReport check(String text) {
        Objects.requireNonNull(text, "text");
        EvaluationContext context = EvaluationContext.current();

        List<Reference> references = extractor.extract(text);
        List<ResolvedReference> resolved = resolver.resolve(references);

        // Document order of first occurrence, so the report reads down the page the way the
        // document does. A source cited twice is one row, keyed on what it resolved to and
        // labelled with the first raw text that named it.
        Map<String, ResolvedReference> decisions = new LinkedHashMap<>();
        Map<Long, ResolvedReference> citedProvisions = new LinkedHashMap<>();
        Set<UnresolvedReference> unresolved = new LinkedHashSet<>();
        List<SourceKey> order = new ArrayList<>();

        for (ResolvedReference reference : resolved) {
            String ecli = reference.ecli();
            Long provisionId = reference.provisionId();
            String reason = reference.unresolvedReason();
            if (ecli != null) {
                if (decisions.putIfAbsent(ecli, reference) == null) {
                    order.add(new SourceKey.Decision(ecli));
                }
            } else if (provisionId != null) {
                if (citedProvisions.putIfAbsent(provisionId, reference) == null) {
                    order.add(new SourceKey.Provision(provisionId));
                }
            } else if (reason != null) {
                unresolved.add(new UnresolvedReference(reference.rawText(), reason));
            }
        }

        Map<String, Evaluated> decisionStatuses = statuses.evaluateAll(decisions.keySet());
        Map<Long, ProvisionEvidence> provisionEvidence =
                provisions.directlyCited(citedProvisions.keySet(), context.asOf());

        List<SourceReport> sources = new ArrayList<>(order.size());
        for (SourceKey key : order) {
            sources.add(switch (key) {
                case SourceKey.Decision(String ecli) ->
                        decisionSource(ecli, decisions, decisionStatuses);
                case SourceKey.Provision(long provisionId) ->
                        provisionSource(provisionId, citedProvisions, provisionEvidence);
            });
        }

        return new DocumentReport(
                context.asOf(),
                context.corpus().get().courts(),
                sources,
                List.copyOf(unresolved));
    }

    /**
     * What a report row is keyed on. A decision and a provision are the two kinds of source
     * a document can rely on, and the report lists them interleaved in document order.
     */
    private sealed interface SourceKey {

        record Decision(String ecli) implements SourceKey {}

        record Provision(long id) implements SourceKey {}
    }

    private static SourceReport decisionSource(
            String ecli,
            Map<String, ResolvedReference> decisions,
            Map<String, Evaluated> statuses) {
        ResolvedReference reference = Objects.requireNonNull(decisions.get(ecli));
        Evaluated evaluated = statuses.get(ecli);
        if (evaluated == null) {
            // Cannot happen: every requested ECLI is answered. Fail rather than invent a
            // light, because a light with no evaluation behind it is exactly what hard
            // rule 2 forbids.
            throw new IllegalStateException("no evaluation for " + ecli);
        }
        return new SourceReport(
                ecli, reference.rawText(), evaluated.status().light(), evaluated.reasons());
    }

    private SourceReport provisionSource(
            long provisionId,
            Map<Long, ResolvedReference> citedProvisions,
            Map<Long, ProvisionEvidence> provisionEvidence) {
        ResolvedReference reference = Objects.requireNonNull(citedProvisions.get(provisionId));
        ProvisionEvidence evidence = provisionEvidence.get(provisionId);
        if (evidence == null) {
            // The provision resolved to an id but has no version rows to judge it by. GREEN
            // with no reasons is the honest answer: nothing adverse was found because there
            // was nothing to look at.
            return new SourceReport(null, reference.rawText(), Light.GREEN, List.of());
        }
        Evaluated evaluated = statuses.evaluateProvision(reference.rawText(), evidence);
        return new SourceReport(
                null, reference.rawText(), evaluated.status().light(), evaluated.reasons());
    }
}

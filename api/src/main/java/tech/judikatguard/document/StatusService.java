package tech.judikatguard.document;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import org.springframework.stereotype.Service;
import tech.judikatguard.decision.Corpus;
import tech.judikatguard.decision.EvidenceRepository;
import tech.judikatguard.decision.ProvisionRepository;
import tech.judikatguard.status.Evaluation;
import tech.judikatguard.status.EvidenceRow;
import tech.judikatguard.status.InheritedEvidence;
import tech.judikatguard.status.Light;
import tech.judikatguard.status.ProvisionEvidence;
import tech.judikatguard.status.Status;
import tech.judikatguard.status.StatusEngine;
import tech.judikatguard.status.Thresholds;

/**
 * Assembles an {@link Evaluation} from the graph and hands it to {@link StatusEngine}.
 *
 * <p>This class does the reading; the engine does the deciding. Nothing here assigns a
 * light, and nothing in the engine reads a repository (D2). The split is what lets the
 * verdict policy change without re-running a single inference, and what lets the truth
 * table test the policy without a database.
 *
 * <p>Verdicts are computed on read rather than materialised (D4): a status is a handful of
 * indexed queries plus a pure function, and computing on demand removes a whole class of
 * staleness bugs.
 */
@Service
public final class StatusService {

    private final EvidenceRepository evidence;
    private final ProvisionRepository provisions;

    public StatusService(EvidenceRepository evidence, ProvisionRepository provisions) {
        this.evidence = Objects.requireNonNull(evidence, "evidence");
        this.provisions = Objects.requireNonNull(provisions, "provisions");
    }

    /**
     * Evaluate one decision. The ECLI is not checked for existence: an ECLI with no evidence
     * against it is GREEN, and telling the two states apart is the caller's job (the
     * controller answers 404 for an ECLI the corpus has never heard of).
     */
    public Evaluated evaluate(String ecli) {
        return Objects.requireNonNull(
                evaluateAll(List.of(ecli)).get(ecli),
                "evaluateAll must answer for every requested ecli");
    }

    /**
     * Evaluate a batch, which is what an uploaded document needs. Five queries in total
     * regardless of how many sources the document cites.
     *
     * <p>The one-hop propagation is resolved here rather than in SQL or in the engine. The
     * sources each decision wholly relies on are evaluated first, <em>without</em> inherited
     * evidence of their own, and only their lights are passed on. That is what makes the hop
     * count exactly one: there is no path by which a third-level source can influence the
     * answer, whatever the shape of the graph, including a cycle.
     */
    public Map<String, Evaluated> evaluateAll(Collection<String> eclis) {
        if (eclis.isEmpty()) {
            return Map.of();
        }
        EvaluationContext context = EvaluationContext.current();
        LocalDate asOf = context.asOf();
        Corpus corpus = context.corpus().get();

        Set<String> requested = new LinkedHashSet<>(eclis);
        Map<String, List<String>> followed = evidence.whollyReliedOn(requested);

        Set<String> everything = new LinkedHashSet<>(requested);
        followed.values().forEach(everything::addAll);

        Map<String, List<EvidenceRow>> treatments = evidence.treatmentsFor(everything);
        Map<String, List<ProvisionEvidence>> reliedOn = provisions.reliedOn(everything, asOf);

        // The hop. Each source's own direct evidence only; its inherited list stays empty.
        Map<String, Light> hopLights = new LinkedHashMap<>();
        for (String via : everything) {
            hopLights.put(
                    via,
                    StatusEngine.evaluate(evaluation(
                                    via, asOf, corpus, treatments, reliedOn, List.of()))
                            .light());
        }

        Map<String, Evaluated> evaluated = new LinkedHashMap<>();
        for (String ecli : requested) {
            List<InheritedEvidence> inherited = new ArrayList<>();
            for (String via : followed.getOrDefault(ecli, List.of())) {
                inherited.add(new InheritedEvidence(via, hopLights.getOrDefault(via, Light.GREEN)));
            }
            List<EvidenceRow> rows = treatments.getOrDefault(ecli, List.of());
            Status status = StatusEngine.evaluate(
                    evaluation(ecli, asOf, corpus, treatments, reliedOn, inherited));
            evaluated.put(ecli, new Evaluated(status, rows));
        }
        return Map.copyOf(evaluated);
    }

    /**
     * The verdict for a provision cited directly in an uploaded document.
     *
     * <p>Routed through the engine like everything else, so that "a derogated provision is
     * red" is stated in exactly one place. {@code reference} is the citation as the document
     * wrote it and is used only as the identity of this evaluation; it is never emitted as
     * an ECLI, because a provision does not have one.
     */
    public Evaluated evaluateProvision(String reference, ProvisionEvidence provision) {
        EvaluationContext context = EvaluationContext.current();
        Corpus corpus = context.corpus().get();
        LocalDate asOf = context.asOf();
        Status status = StatusEngine.evaluate(new Evaluation(
                reference,
                asOf,
                corpus.size(),
                corpus.through(asOf),
                List.of(),
                List.of(provision),
                List.of(),
                Thresholds.defaults()));
        return new Evaluated(status, List.of());
    }

    private static Evaluation evaluation(
            String ecli,
            LocalDate asOf,
            Corpus corpus,
            Map<String, List<EvidenceRow>> treatments,
            Map<String, List<ProvisionEvidence>> reliedOn,
            List<InheritedEvidence> inherited) {
        return new Evaluation(
                ecli,
                asOf,
                corpus.size(),
                corpus.through(asOf),
                treatments.getOrDefault(ecli, List.of()),
                reliedOn.getOrDefault(ecli, List.of()),
                inherited,
                Thresholds.defaults());
    }

    /**
     * A {@link Status} together with the treatment rows it was computed from.
     *
     * <p>The rows travel with the status because the wire format carries a paragraph index
     * per reason and {@link tech.judikatguard.status.Reason} does not: the engine has no use
     * for it. Handing the rows back is cheaper and more honest than a second query.
     */
    public record Evaluated(Status status, List<EvidenceRow> rows) {

        public Evaluated {
            Objects.requireNonNull(status, "status");
            rows = List.copyOf(rows);
        }

        public List<ReasonView> reasons() {
            return ReasonView.of(status.reasons(), rows);
        }
    }
}

package tech.judikatguard.document;

import java.util.List;
import java.util.Objects;
import java.util.Optional;
import org.springframework.stereotype.Service;
import tech.judikatguard.decision.CitationRepository;
import tech.judikatguard.decision.CitationRepository.CitationRow;
import tech.judikatguard.decision.DecisionRepository;
import tech.judikatguard.decision.DecisionSummary;
import tech.judikatguard.llm.PropositionChecker;
import tech.judikatguard.llm.PropositionVerdict;

/**
 * {@code POST /api/citations/{id}/proposition-check}. PLAN.md section 12.
 *
 * <p>Not "is this source current" but "are you using it correctly". The reading is done
 * here; the model call, the evidence-span gate and the cache are
 * {@link PropositionChecker}'s.
 *
 * <p>The material handed to the model is the cited decision's own <em>právní věta</em> and
 * its own paragraphs, and nothing else. That is what makes the returned
 * {@code evidence_span} checkable: it can only be verbatim text from a decision that is
 * really in the corpus.
 */
@Service
public final class PropositionService {

    private final CitationRepository citations;
    private final DecisionRepository decisions;
    private final PropositionChecker checker;

    public PropositionService(
            CitationRepository citations,
            DecisionRepository decisions,
            PropositionChecker checker) {
        this.citations = Objects.requireNonNull(citations, "citations");
        this.decisions = Objects.requireNonNull(decisions, "decisions");
        this.checker = Objects.requireNonNull(checker, "checker");
    }

    /**
     * @return empty when the citation id is unknown, or when it names a provision rather
     *     than a decision — a proposition check compares a claim against what a court held,
     *     and a statutory text holds nothing
     * @throws tech.judikatguard.llm.LlmClient.Unavailable when no model call can be made,
     *     which the caller reports as unchecked rather than as a verdict
     */
    public Optional<PropositionVerdict> check(long citationId, String claim) {
        Objects.requireNonNull(claim, "claim");
        Optional<CitationRow> citation = citations.find(citationId);
        if (citation.isEmpty()) {
            return Optional.empty();
        }
        String citedEcli = citation.get().citedEcli();
        if (citedEcli == null) {
            return Optional.empty();
        }
        Optional<DecisionSummary> cited = decisions.find(citedEcli);
        if (cited.isEmpty()) {
            return Optional.empty();
        }
        DecisionSummary decision = cited.get();
        List<String> paragraphs = decisions.paragraphs(citedEcli);
        return Optional.of(checker.check(
                decision.ecli(),
                decision.courtCode(),
                decision.decidedOn(),
                decision.ratioOrEmpty(),
                String.join("\n\n", paragraphs),
                claim));
    }
}

package tech.judikatguard.web;

import jakarta.validation.Valid;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import tech.judikatguard.decision.DecisionRepository;
import tech.judikatguard.document.PropositionService;
import tech.judikatguard.document.ReasonView;
import tech.judikatguard.document.StatusService;
import tech.judikatguard.llm.PropositionVerdict;

/**
 * One decision: its metadata, its traffic light, and whether a claim it is cited for is
 * actually what it held.
 *
 * <p>Every endpoint here 404s for an ECLI the corpus has never seen. That is a deliberately
 * different answer from a green light: "we have no record of this" must never be able to
 * read as "we found nothing against this".
 *
 * <p>{@code ?asOf=} is not a parameter of these methods. It is read once per request by
 * {@link EvaluationContextFilter} and carried in the {@code ScopedValue}, so that every
 * part of an answer is computed against the same date.
 */
@RestController
@RequestMapping("/api/decisions")
public class DecisionController {

    private final DecisionRepository decisions;
    private final StatusService statuses;
    private final PropositionService propositions;

    public DecisionController(
            DecisionRepository decisions,
            StatusService statuses,
            PropositionService propositions) {
        this.decisions = Objects.requireNonNull(decisions, "decisions");
        this.statuses = Objects.requireNonNull(statuses, "statuses");
        this.propositions = Objects.requireNonNull(propositions, "propositions");
    }

    @GetMapping("/{ecli}")
    DecisionView decision(@PathVariable String ecli) {
        return decisions.find(ecli).map(DecisionView::of).orElseThrow(() -> notFound(ecli));
    }

    @GetMapping("/{ecli}/status")
    StatusView status(@PathVariable String ecli) {
        // Existence is checked before evaluating: an ECLI with no evidence against it is
        // GREEN, and an ECLI that does not exist is a 404. The two must not collapse.
        if (decisions.find(ecli).isEmpty()) {
            throw notFound(ecli);
        }
        StatusService.Evaluated evaluated = statuses.evaluate(ecli);
        Set<String> linked = new LinkedHashSet<>(List.of(ecli));
        linked.addAll(ReasonView.linkTargets(evaluated.reasons()));
        return StatusView.of(evaluated, decisions.sourceUrls(linked));
    }

    /**
     * {@code POST /api/decisions/{ecli}/proposition-check}: not "is this source current" but
     * "are you using it correctly" (PLAN.md section 12).
     *
     * <p>Addressed by ECLI because that is what an uploaded document has. The web page reads
     * the ECLI off a source row of its own report and asks about the claim the user typed;
     * there is no {@code citation} row to name, and deliberately so.
     *
     * <p>An {@code UNCLASSIFIED} verdict is a 200. The model answered and the answer could
     * not be validated, which is a result the user needs to see rather than a server error.
     * A model that cannot be reached at all is a 503, handled by {@link ApiExceptionHandler}.
     */
    @PostMapping("/{ecli}/proposition-check")
    PropositionVerdict propositionCheck(
            @PathVariable String ecli, @Valid @RequestBody ClaimRequest request) {
        return propositions.checkDecision(ecli, request.claim()).orElseThrow(() -> notFound(ecli));
    }

    private static NotFoundException notFound(String ecli) {
        return new NotFoundException(
                "Rozhodnutí " + ecli + " není v korpusu. To neznamená, že je bez vady — "
                        + "znamená to, že o něm nemáme žádný záznam.");
    }
}

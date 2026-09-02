package tech.judikatguard.web;

import java.util.Objects;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import tech.judikatguard.decision.DecisionRepository;
import tech.judikatguard.document.StatusService;

/**
 * One decision: its metadata, and its traffic light.
 *
 * <p>Both endpoints 404 for an ECLI the corpus has never seen. That is a deliberately
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

    public DecisionController(DecisionRepository decisions, StatusService statuses) {
        this.decisions = Objects.requireNonNull(decisions, "decisions");
        this.statuses = Objects.requireNonNull(statuses, "statuses");
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
        return StatusView.of(statuses.evaluate(ecli));
    }

    private static NotFoundException notFound(String ecli) {
        return new NotFoundException(
                "Rozhodnutí " + ecli + " není v korpusu. To neznamená, že je bez vady — "
                        + "znamená to, že o něm nemáme žádný záznam.");
    }
}

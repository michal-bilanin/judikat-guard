package tech.judikatguard.web;

import jakarta.validation.Valid;
import java.util.Objects;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import tech.judikatguard.document.PropositionService;
import tech.judikatguard.llm.PropositionVerdict;

/**
 * {@code POST /api/citations/{id}/proposition-check}. PLAN.md section 12.
 *
 * <p>The one endpoint that asks whether the source is being used correctly rather than
 * whether it is current. An {@code UNCLASSIFIED} verdict comes back with a 200: the model
 * answered and the answer could not be trusted, which is a result the user needs to see,
 * not a server error.
 */
@RestController
@RequestMapping("/api/citations")
public class CitationController {

    private final PropositionService propositions;

    public CitationController(PropositionService propositions) {
        this.propositions = Objects.requireNonNull(propositions, "propositions");
    }

    @PostMapping("/{id}/proposition-check")
    PropositionVerdict propositionCheck(
            @PathVariable long id, @Valid @RequestBody ClaimRequest request) {
        return propositions.check(id, request.claim())
                .orElseThrow(() -> new NotFoundException(
                        "Citace č. " + id + " neexistuje, nebo neodkazuje na rozhodnutí soudu. "
                                + "Tvrzení lze porovnat jen se závěrem rozhodnutí."));
    }
}

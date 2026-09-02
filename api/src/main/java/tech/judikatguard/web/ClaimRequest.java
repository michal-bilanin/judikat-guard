package tech.judikatguard.web;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * {@code POST /api/citations/{id}/proposition-check} body: the claim the document offers the
 * cited decision for. PLAN.md section 12.
 */
public record ClaimRequest(
        @NotBlank(message = "Tvrzení nesmí být prázdné.")
        @Size(max = 4_000, message = "Tvrzení je příliš dlouhé; uveďte jedno tvrzení.")
        String claim) {}

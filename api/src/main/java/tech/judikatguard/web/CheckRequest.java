package tech.judikatguard.web;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * {@code POST /api/documents/check} body. Matches {@code CheckRequest} in
 * {@code web/src/types.ts}.
 *
 * <p>The text is extracted from and thrown away. Nothing about an uploaded document is
 * stored: not the text, not its citations, not a report (PLAN.md section 11).
 */
public record CheckRequest(
        @NotBlank(message = "Text dokumentu nesmí být prázdný.")
        @Size(max = 2_000_000, message = "Text dokumentu je příliš dlouhý.")
        String text) {}

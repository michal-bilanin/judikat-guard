package tech.judikatguard.web;

import java.util.stream.Collectors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import tech.judikatguard.llm.LlmClient;

/**
 * Error responses, as {@code application/problem+json} with Czech detail text.
 *
 * <p>The distinction that matters here is between "we could not answer" and "the answer is
 * that nothing was found". A model that is unreachable, a citation that does not resolve and
 * a decision that is not in the corpus are all reported as themselves; none of them is
 * allowed to degrade into a green light (CLAUDE.md rule 2).
 */
@RestControllerAdvice
public class ApiExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ApiExceptionHandler.class);

    @ExceptionHandler(NotFoundException.class)
    ProblemDetail notFound(NotFoundException e) {
        return problem(HttpStatus.NOT_FOUND, Wording.NOT_FOUND_TITLE, e.getMessage());
    }

    /**
     * No model call could be made: no API key, transport failure, an error status. Reported
     * as unavailable, never as a verdict. Distinct from {@code UNCLASSIFIED}, where a reply
     * did arrive and failed the evidence-span gate.
     */
    @ExceptionHandler(LlmClient.Unavailable.class)
    ProblemDetail unavailable(LlmClient.Unavailable e) {
        log.warn("model call unavailable: {}", e.getMessage());
        return problem(
                HttpStatus.SERVICE_UNAVAILABLE,
                Wording.LLM_UNAVAILABLE_TITLE,
                "Ověření tvrzení není momentálně dostupné, protože nelze provést volání "
                        + "modelu. Ostatní kontroly tím nejsou dotčeny.");
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    ProblemDetail invalid(MethodArgumentNotValidException e) {
        String detail = e.getBindingResult().getFieldErrors().stream()
                .map(error -> error.getDefaultMessage() == null
                        ? error.getField() + ": neplatná hodnota"
                        : error.getDefaultMessage())
                .distinct()
                .collect(Collectors.joining(" "));
        return problem(
                HttpStatus.BAD_REQUEST,
                Wording.BAD_REQUEST_TITLE,
                detail.isBlank() ? "Požadavek neprošel validací." : detail);
    }

    @ExceptionHandler(HttpMessageNotReadableException.class)
    ProblemDetail unreadable(HttpMessageNotReadableException e) {
        return problem(
                HttpStatus.BAD_REQUEST,
                Wording.BAD_REQUEST_TITLE,
                "Tělo požadavku nelze přečíst; očekáván JSON objekt.");
    }

    private static ProblemDetail problem(HttpStatus status, String title, String detail) {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(status, detail);
        problem.setTitle(title);
        return problem;
    }
}

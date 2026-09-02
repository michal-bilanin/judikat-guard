package tech.judikatguard.web;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.Objects;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ProblemDetail;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;
import tech.judikatguard.decision.CorpusRepository;
import tech.judikatguard.document.EvaluationContext;
import tech.judikatguard.llm.PromptTemplate;
import tech.judikatguard.llm.PropositionChecker;
import tools.jackson.databind.ObjectMapper;

/**
 * Binds the {@link EvaluationContext} for the duration of one request.
 *
 * <p>This is the single place the read path learns what date it is answering about. The
 * {@code asOf} query parameter is read here rather than in each controller, so that every
 * part of one report is computed against the same date — a report whose halves disagreed
 * about "now" would be indefensible, and threading the parameter by hand is exactly how
 * that happens.
 *
 * <p>The clock read also lives here, at the edge, which is what keeps
 * {@link tech.judikatguard.status.StatusEngine} free of one.
 *
 * <p>The corpus figures are loaded lazily and at most once per request, so
 * {@code GET /api/health} still costs no query.
 */
@Component
@Order(EvaluationContextFilter.ORDER)
public final class EvaluationContextFilter extends OncePerRequestFilter {

    /** Early, so that anything downstream can read the context. */
    public static final int ORDER = 0;

    /** {@code ?asOf=2026-09-01}. Absent means today. */
    public static final String AS_OF_PARAMETER = "asOf";

    private final CorpusRepository corpus;
    private final ObjectMapper json;
    private final Clock clock;
    private final String promptVersion;

    public EvaluationContextFilter(CorpusRepository corpus, ObjectMapper json) {
        this.corpus = Objects.requireNonNull(corpus, "corpus");
        this.json = Objects.requireNonNull(json, "json");
        // The one clock read in the whole read path, at the edge. That is what keeps
        // StatusEngine free of one.
        this.clock = Clock.systemDefaultZone();
        // Read once, from the file: the active prompt version is a property of the
        // deployment and hardcoding it is what makes a stored prompt_version a lie
        // (CLAUDE.md rule 7).
        this.promptVersion = PromptTemplate.load(PropositionChecker.PROMPT_FILE).version();
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {

        String raw = request.getParameter(AS_OF_PARAMETER);
        LocalDate asOf;
        try {
            asOf = raw == null || raw.isBlank() ? LocalDate.now(clock) : LocalDate.parse(raw.strip());
        } catch (DateTimeParseException e) {
            // Answered here rather than raised: a filter runs before the dispatcher, so a
            // @RestControllerAdvice would never see it.
            rejectAsOf(response, raw);
            return;
        }

        EvaluationContext context =
                EvaluationContext.lazy(asOf, promptVersion, corpus::coverage);
        try {
            context.callWith(() -> {
                chain.doFilter(request, response);
                return null;
            });
        } catch (IOException | ServletException | RuntimeException e) {
            throw e;
        } catch (Exception e) {
            // ScopedValue.Carrier#call widens the thrown type. Nothing downstream of a
            // servlet filter throws a checked exception that is not one of the two above.
            throw new ServletException(e);
        }
    }

    private void rejectAsOf(HttpServletResponse response, String raw) throws IOException {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(
                HttpStatus.BAD_REQUEST,
                "Parametr asOf musí být datum ve formátu RRRR-MM-DD. Obdrženo: " + raw);
        problem.setTitle(Wording.BAD_REQUEST_TITLE);
        response.setStatus(HttpStatus.BAD_REQUEST.value());
        response.setContentType(MediaType.APPLICATION_PROBLEM_JSON_VALUE);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.getWriter().write(json.writeValueAsString(problem));
    }
}

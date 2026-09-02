package tech.judikatguard.document;

import java.time.LocalDate;
import java.util.Objects;
import java.util.function.Supplier;
import org.jspecify.annotations.Nullable;
import tech.judikatguard.decision.Corpus;

/**
 * Request-scoped evaluation context, carried by a {@link ScopedValue} (JEP 506, final in
 * Java 25 — no preview flag).
 *
 * <p>Three facts belong to a request rather than to a call: the date the question is asked
 * about, how much corpus the answer is scoped to, and which prompt version a query-time
 * model call runs. Threading them through every repository signature would put the same
 * three parameters on a dozen methods and invite a caller to pass a different {@code asOf}
 * to two halves of one report. Binding them once, in a servlet filter, makes that
 * impossible (PLAN.md section 4).
 *
 * <p><b>Never read inside {@code tech.judikatguard.status}.</b> The engine takes an
 * {@link tech.judikatguard.status.Evaluation} and nothing else, so the truth table can call
 * it directly and so a verdict is reproducible from its inputs alone. Scoped values are for
 * the plumbing around the engine.
 *
 * @param asOf the date the question is asked about; defaults to today, set by the caller
 *     with {@code ?asOf=}
 * @param promptVersion the prompt version a query-time model call records on the rows it
 *     produces
 */
public record EvaluationContext(LocalDate asOf, String promptVersion, Supplier<Corpus> corpus) {

    private static final ScopedValue<EvaluationContext> CURRENT = ScopedValue.newInstance();

    public EvaluationContext {
        Objects.requireNonNull(asOf, "asOf");
        Objects.requireNonNull(promptVersion, "promptVersion");
        Objects.requireNonNull(corpus, "corpus");
    }

    /**
     * A context whose corpus figures are read at most once, and only if something asks for
     * them. {@code GET /api/health} must not cost a query.
     */
    public static EvaluationContext lazy(
            LocalDate asOf, String promptVersion, Supplier<Corpus> loader) {
        return new EvaluationContext(asOf, promptVersion, memoize(loader));
    }

    /** Runs {@code body} with this context bound. The only way to bind one. */
    public <R, X extends Throwable> R callWith(ScopedValue.CallableOp<? extends R, X> body)
            throws X {
        return ScopedValue.where(CURRENT, this).call(body);
    }

    /**
     * The bound context.
     *
     * @throws IllegalStateException if nothing bound one, which means the read path was
     *     entered from outside a request without saying which date it is about. Failing
     *     loudly beats silently answering about today.
     */
    public static EvaluationContext current() {
        return CURRENT.orElseThrow(() -> new IllegalStateException(
                "no EvaluationContext is bound; the read path must run inside "
                        + "EvaluationContext.callWith"));
    }

    private static Supplier<Corpus> memoize(Supplier<Corpus> loader) {
        Objects.requireNonNull(loader, "loader");
        // Confined to one request, so a plain field is enough; the double read a race could
        // cause would only repeat an idempotent query.
        return new Supplier<>() {
            private @Nullable Corpus value;

            @Override
            public Corpus get() {
                Corpus loaded = value;
                if (loaded == null) {
                    loaded = loader.get();
                    value = loaded;
                }
                return loaded;
            }
        };
    }
}

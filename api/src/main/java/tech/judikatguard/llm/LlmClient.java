package tech.judikatguard.llm;

import java.util.Objects;

/**
 * One model call: a fully rendered prompt in, the model's raw reply out.
 *
 * <p>Deliberately the narrowest surface that the two callers in this repository need
 * (PLAN.md section 4: a plain client behind this interface, not a framework). Parsing,
 * validation, retrying and caching are the caller's job, because those are the parts that
 * carry the project's rules and they must be testable without a network.
 *
 * <p>Implementations call at temperature 0 where the model still accepts the parameter and
 * are otherwise deterministic in configuration, so that a cached reply is a faithful replay
 * rather than an approximation (CLAUDE.md rule 7).
 */
public interface LlmClient {

    /**
     * Sends one single-turn prompt and returns the concatenated text of the reply.
     *
     * @throws Unavailable when no reply could be obtained: no API key, transport failure,
     *     an error status, a truncated reply, or the model declining the request
     */
    String complete(String prompt);

    /**
     * The model identifier to record in the {@code model} column of every row this client's
     * output produces. Never throws, so it is safe to use in a log or an error message.
     */
    String model();

    /**
     * No reply is available. One catchable type for every reason, because every caller
     * reacts the same way: report the source as unchecked rather than guess at a verdict.
     *
     * <p>Distinct from a rejected reply. A reply that arrives and fails the evidence-span
     * gate is not an {@code Unavailable}: it is retried once and then recorded as
     * {@code UNCLASSIFIED}.
     */
    final class Unavailable extends RuntimeException {

        public Unavailable(String message) {
            super(message);
        }

        public Unavailable(String message, Throwable cause) {
            super(message, cause);
        }
    }

    /**
     * The fallback used when no API key is configured. Every call fails the same way, so the
     * application starts, Flyway runs, and every endpoint that does not need a model call
     * keeps working; only the two model-backed features report themselves as unavailable.
     *
     * @param reason stated verbatim in the thrown {@link Unavailable}
     */
    record Unconfigured(String reason) implements LlmClient {

        public Unconfigured {
            Objects.requireNonNull(reason, "reason");
        }

        @Override
        public String complete(String prompt) {
            throw new Unavailable(reason);
        }

        /** The sentinel written nowhere: an unconfigured client never produces a row. */
        @Override
        public String model() {
            return "unavailable";
        }
    }
}

package tech.judikatguard.llm;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.TreeSet;
import java.util.concurrent.ThreadLocalRandom;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Condition;
import org.springframework.context.annotation.ConditionContext;
import org.springframework.context.annotation.Conditional;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.core.env.Environment;
import org.springframework.core.type.AnnotatedTypeMetadata;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestClientResponseException;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * The Google Gemini Interactions API over a plain {@link RestClient}. The second provider
 * behind {@link LlmClient}, added because Google AI Studio offers a free API tier; the
 * Anthropic path is untouched and still selectable.
 *
 * <p>One POST to {@code /v1beta/interactions}, one JSON body, one text reply. No SDK, for
 * the same reason {@link AnthropicLlmClient} has none (PLAN.md section 4).
 *
 * <p>Configuration is optional and resolves from the environment through Spring's relaxed
 * binding. Property names and defaults are {@link Settings#fromEnvironment}, which is the
 * only place they can be wrong.
 *
 * <p><b>Provider precedence.</b> Exactly one {@link LlmClient} must win, deterministically:
 *
 * <ol>
 *   <li>{@code judikatguard.llm.provider=gemini} selects this client and nothing else can
 *       override it. If {@code gemini.api-key} is then missing, the bean is
 *       {@link LlmClient.Unconfigured} rather than a silent fall back to Anthropic: an
 *       explicit choice is honoured or reported, never quietly ignored.</li>
 *   <li>Any other explicit value ({@code anthropic}) leaves this client unregistered.</li>
 *   <li>With no explicit provider, this client registers only when {@code gemini.api-key} is
 *       set and {@code anthropic.api-key} is not. Both keys and no provider therefore keeps
 *       the pre-existing Anthropic behaviour.</li>
 * </ol>
 *
 * <p>The bean is {@link Primary} so that the resolution never depends on the order in which
 * Spring happens to process this {@code @Configuration} and {@link AnthropicLlmClient.Config}
 * — the latter's {@code @ConditionalOnMissingBean} fallback may register first, and injection
 * points must still get this client rather than an ambiguity error. With neither key and no
 * provider set, nothing here registers and that fallback remains the only {@code LlmClient},
 * so the application still boots.
 *
 * <p><b>On temperature.</b> CLAUDE.md rule 7 asks for temperature 0. Gemini still accepts the
 * sampling parameter, so this provider satisfies the rule to the letter: {@code temperature:
 * 0} is sent explicitly inside {@code generation_config} on every call. This is the one
 * difference worth noting against {@link AnthropicLlmClient}, which had to document a
 * deviation because the current Claude models removed the parameter and reject it outright.
 * Thinking cannot be switched off on Gemini 3.x; {@value #DEFAULT_THINKING_LEVEL} is the
 * floor, and it is the right setting for a schema-constrained classification whose thinking
 * tokens are billed as output.
 *
 * <p><b>On retrying.</b> The free tier meters requests per minute, tokens per minute and
 * requests per day at once, and breaching any one of the three returns 429. A batch that dies
 * on an unhandled 429 wastes every call before it, so 429 and 5xx are retried here with
 * {@code Retry-After} honoured when present and exponential backoff with jitter otherwise.
 * This is transport-level retrying only. It is unrelated to, and sits below, the single
 * evidence-span retry in {@link PropositionChecker}, which is what CLAUDE.md rule 3 governs.
 */
public final class GeminiLlmClient implements LlmClient {

    /**
     * Google's "fastest, most cost-effective 3.5 model for high-throughput execution", which
     * is what this workload is. Free-tier availability per model is undocumented and shifts,
     * which is exactly why this is the single place a model id is written down.
     */
    public static final String DEFAULT_MODEL = "gemini-3.5-flash-lite";

    public static final String DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com";

    /** {@code minimal} | {@code low} | {@code medium} | {@code high}. */
    public static final String DEFAULT_THINKING_LEVEL = "minimal";

    public static final int DEFAULT_TIMEOUT_SECONDS = 120;

    /** One call plus four retries: enough to ride out a per-minute cap, not a daily one. */
    public static final int DEFAULT_MAX_ATTEMPTS = 5;

    public static final long DEFAULT_RETRY_INITIAL_MILLIS = 1_000;

    static final String API_KEY_PROPERTY = "gemini.api-key";

    static final String PROVIDER_PROPERTY = "judikatguard.llm.provider";

    static final String PROVIDER_GEMINI = "gemini";

    static final String API_KEY_HEADER = "x-goog-api-key";

    static final String INTERACTIONS_PATH = "/v1beta/interactions";

    /** No single backoff wait exceeds this, whatever the attempt number. */
    private static final Duration MAX_BACKOFF = Duration.ofSeconds(60);

    /** A {@code Retry-After} longer than this is treated as a lost cause, not obeyed. */
    private static final Duration MAX_RETRY_AFTER = Duration.ofSeconds(300);

    private static final Logger log = LoggerFactory.getLogger(GeminiLlmClient.class);

    /** The real one. Tests supply a recording no-op so that no test sleeps. */
    static final Pause SLEEPING = duration -> {
        try {
            Thread.sleep(duration);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new Unavailable("interrupted while backing off before a Gemini retry", e);
        }
    };

    private final RestClient http;
    private final ObjectMapper json;
    private final String model;
    private final String thinkingLevel;
    private final @Nullable Map<String, Object> responseSchema;
    private final int maxAttempts;
    private final Duration retryInitial;
    private final Pause pause;

    /**
     * @param http already carrying the base URL and the {@value #API_KEY_HEADER} header
     * @param responseSchema the JSON Schema to constrain the reply with, or null to ask only
     *     for the {@code application/json} mime type
     * @param pause how a backoff is served; {@link #SLEEPING} outside tests
     */
    public GeminiLlmClient(
            RestClient http,
            ObjectMapper json,
            String model,
            String thinkingLevel,
            @Nullable Map<String, Object> responseSchema,
            int maxAttempts,
            Duration retryInitial,
            Pause pause) {
        this.http = Objects.requireNonNull(http, "http");
        this.json = Objects.requireNonNull(json, "json");
        this.model = Objects.requireNonNull(model, "model");
        this.thinkingLevel = Objects.requireNonNull(thinkingLevel, "thinkingLevel");
        this.responseSchema = responseSchema;
        this.retryInitial = Objects.requireNonNull(retryInitial, "retryInitial");
        this.pause = Objects.requireNonNull(pause, "pause");
        if (maxAttempts < 1) {
            throw new IllegalArgumentException("maxAttempts must be at least 1, got " + maxAttempts);
        }
        this.maxAttempts = maxAttempts;
    }

    public GeminiLlmClient(Settings settings, ObjectMapper json) {
        this(
                restClient(settings),
                json,
                settings.model(),
                settings.thinkingLevel(),
                settings.propositionSchema() ? propositionResponseSchema() : null,
                settings.maxAttempts(),
                settings.retryInitial(),
                SLEEPING);
    }

    @Override
    public String model() {
        return model;
    }

    @Override
    public String complete(String prompt) {
        String body = json.writeValueAsString(requestBody(prompt));
        for (int attempt = 1; ; attempt++) {
            try {
                return textOf(http.post()
                        .uri(INTERACTIONS_PATH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .body(body)
                        .retrieve()
                        .requiredBody(String.class));
            } catch (RestClientResponseException e) {
                // The status body carries Google's own error message, which is the difference
                // between "fix your key", "that schema is too big" and "you are over the
                // per-minute cap, wait".
                if (!isRetryable(e.getStatusCode()) || attempt >= maxAttempts) {
                    throw new Unavailable(
                            "Gemini Interactions API returned " + e.getStatusCode() + " after "
                                    + attempt + " attempt(s): "
                                    + abbreviate(e.getResponseBodyAsString()), e);
                }
                int made = attempt;
                Duration wait = retryAfter(e.getResponseHeaders()).orElseGet(() -> backoff(made));
                log.warn(
                        "Gemini returned {} on attempt {}/{}, retrying in {} ms",
                        e.getStatusCode(),
                        attempt,
                        maxAttempts,
                        wait.toMillis());
                pause.pause(wait);
            } catch (RestClientException e) {
                throw new Unavailable("Gemini Interactions API call failed: " + e.getMessage(), e);
            }
        }
    }

    /**
     * The Interactions body. {@code input} takes the whole prompt as one string, which is
     * exactly the shape {@link LlmClient#complete} already has, and {@code temperature} and
     * {@code thinking_level} nest inside {@code generation_config} rather than sitting at the
     * top level.
     */
    private Map<String, Object> requestBody(String prompt) {
        Map<String, Object> generationConfig = new LinkedHashMap<>();
        // CLAUDE.md rule 7, satisfied literally: Gemini still accepts the sampling
        // parameter, so temperature 0 is sent on every call rather than omitted.
        generationConfig.put("temperature", 0);
        generationConfig.put("thinking_level", thinkingLevel);

        Map<String, Object> responseFormat = new LinkedHashMap<>();
        responseFormat.put("type", "text");
        responseFormat.put("mime_type", "application/json");
        if (responseSchema != null) {
            responseFormat.put("schema", responseSchema);
        }

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("model", model);
        body.put("input", prompt);
        body.put("generation_config", generationConfig);
        body.put("response_format", responseFormat);
        return body;
    }

    /**
     * Concatenates the text blocks of the {@code model_output} step. Google's SDKs expose this
     * as {@code output_text}; the raw REST shape is a {@code steps} timeline whose other
     * entries (the echoed {@code user_input}, any tool step) are not the reply and are
     * skipped. A top-level {@code output_text} is accepted as a fallback in case the wire
     * format carries the convenience field too. Nothing else is guessed at: an unrecognised
     * body is reported with a truncated copy of itself rather than reduced to an empty string.
     */
    private String textOf(String raw) {
        JsonNode root;
        try {
            root = json.readTree(raw);
        } catch (JacksonException e) {
            throw new Unavailable("Gemini reply was not JSON: " + abbreviate(raw), e);
        }
        StringBuilder text = new StringBuilder();
        for (JsonNode step : root.path("steps")) {
            if ("model_output".equals(step.path("type").asString(""))) {
                for (JsonNode block : step.path("content")) {
                    if ("text".equals(block.path("type").asString(""))) {
                        text.append(block.path("text").asString(""));
                    }
                }
            }
        }
        if (text.isEmpty()) {
            appendOutputText(root.path("output_text"), text);
        }
        if (text.isEmpty()) {
            throw new Unavailable(
                    "Gemini reply carried no model_output text (status="
                            + root.path("status").asString("null") + "): " + abbreviate(raw));
        }
        return text.toString();
    }

    private static void appendOutputText(JsonNode outputText, StringBuilder into) {
        if (outputText.isString()) {
            into.append(outputText.asString(""));
        } else if (outputText.isArray()) {
            for (JsonNode part : outputText) {
                into.append(part.isString() ? part.asString("") : part.path("text").asString(""));
            }
        }
    }

    /**
     * 429 is the free tier's per-minute, per-token or per-day cap; 5xx is Google's side
     * having a moment. Everything else — a bad key, a rejected schema, a malformed body — is
     * a fault that retrying cannot fix, and repeating it only burns the daily quota.
     */
    private static boolean isRetryable(HttpStatusCode status) {
        return status.value() == 429 || status.is5xxServerError();
    }

    /**
     * Only the delta-seconds form of {@code Retry-After} is honoured. The HTTP-date form is
     * legal but needs a clock read to interpret, and Google sends seconds.
     */
    private static Optional<Duration> retryAfter(@Nullable HttpHeaders headers) {
        String value = headers == null ? null : headers.getFirst(HttpHeaders.RETRY_AFTER);
        if (value == null || value.isBlank()) {
            return Optional.empty();
        }
        long seconds;
        try {
            seconds = Long.parseLong(value.strip());
        } catch (NumberFormatException e) {
            return Optional.empty();
        }
        if (seconds < 0 || Duration.ofSeconds(seconds).compareTo(MAX_RETRY_AFTER) > 0) {
            return Optional.empty();
        }
        return Optional.of(Duration.ofSeconds(seconds));
    }

    /**
     * Exponential with full jitter over the second half of the interval, so that several
     * workers throttled by the same per-project cap do not step back in lockstep.
     */
    private Duration backoff(int attempt) {
        long capped = Math.min(
                retryInitial.toMillis() << Math.min(attempt - 1, 20), MAX_BACKOFF.toMillis());
        long half = capped / 2;
        return Duration.ofMillis(half + ThreadLocalRandom.current().nextLong(half + 1));
    }

    private static String abbreviate(String s) {
        return s.length() <= 500 ? s : s.substring(0, 500) + "…";
    }

    private static RestClient restClient(Settings settings) {
        JdkClientHttpRequestFactory factory = new JdkClientHttpRequestFactory();
        factory.setReadTimeout(settings.readTimeout());
        return RestClient.builder()
                .baseUrl(settings.baseUrl())
                .requestFactory(factory)
                // A header, not a ?key= query parameter: an API key in a URL ends up in every
                // proxy log there is.
                .defaultHeader(API_KEY_HEADER, settings.apiKey())
                .build();
    }

    /**
     * The shape {@code prompts/proposition-check.v1.md} asks for, expressed in the subset of
     * JSON Schema Gemini supports (types, {@code enum}, {@code minimum}/{@code maximum}).
     *
     * <p>The only caller of an {@link LlmClient} in this runtime is {@link PropositionChecker},
     * so constraining the reply here is what makes rule 7's "JSON-schema constrained" true on
     * this provider. The verdicts are read from {@link PropositionVerdict#VERDICTS} rather
     * than written out again, so the schema cannot drift from the validator that rejects a
     * reply. Setting {@code judikatguard.llm.gemini.response-schema=none} drops the schema and
     * asks only for the JSON mime type, which is the escape hatch if a future API build
     * rejects it.
     *
     * <p>{@link PropositionChecker} still validates every field of the reply, and the
     * evidence-span gate still runs. A schema constrains the shape; it cannot make a quote
     * verbatim.
     */
    static Map<String, Object> propositionResponseSchema() {
        Map<String, Object> verdict = new LinkedHashMap<>();
        verdict.put("type", "string");
        verdict.put("enum", List.copyOf(new TreeSet<>(PropositionVerdict.VERDICTS)));

        Map<String, Object> confidence = new LinkedHashMap<>();
        confidence.put("type", "number");
        confidence.put("minimum", 0);
        confidence.put("maximum", 1);

        Map<String, Object> properties = new LinkedHashMap<>();
        properties.put("verdict", verdict);
        properties.put("confidence", confidence);
        properties.put("evidence_span", Map.of("type", "string"));
        properties.put("note", Map.of("type", "string"));

        Map<String, Object> schema = new LinkedHashMap<>();
        schema.put("type", "object");
        schema.put("properties", properties);
        schema.put("required", List.of("verdict", "confidence", "evidence_span", "note"));
        return schema;
    }

    /** How a backoff is served. Injected so that no test ever sleeps. */
    @FunctionalInterface
    public interface Pause {

        void pause(Duration duration);
    }

    /** Everything the client needs to exist, resolved from configuration exactly once. */
    public record Settings(
            String baseUrl,
            String apiKey,
            String model,
            String thinkingLevel,
            boolean propositionSchema,
            int maxAttempts,
            Duration retryInitial,
            Duration readTimeout) {

        public Settings {
            Objects.requireNonNull(baseUrl, "baseUrl");
            Objects.requireNonNull(apiKey, "apiKey");
            Objects.requireNonNull(model, "model");
            Objects.requireNonNull(thinkingLevel, "thinkingLevel");
            Objects.requireNonNull(retryInitial, "retryInitial");
            Objects.requireNonNull(readTimeout, "readTimeout");
            if (maxAttempts < 1) {
                throw new IllegalArgumentException("maxAttempts must be at least 1, got " + maxAttempts);
            }
        }

        public static Settings fromEnvironment(Environment env) {
            return new Settings(
                    env.getProperty("judikatguard.llm.gemini.base-url", DEFAULT_BASE_URL),
                    env.getProperty(API_KEY_PROPERTY, ""),
                    env.getProperty(
                            "judikatguard.llm.gemini.model",
                            env.getProperty("jg.llm.gemini.model", DEFAULT_MODEL)),
                    env.getProperty("judikatguard.llm.gemini.thinking-level", DEFAULT_THINKING_LEVEL),
                    !"none".equalsIgnoreCase(
                            env.getProperty("judikatguard.llm.gemini.response-schema", "proposition")),
                    env.getProperty(
                            "judikatguard.llm.gemini.max-attempts", Integer.class, DEFAULT_MAX_ATTEMPTS),
                    Duration.ofMillis(env.getProperty(
                            "judikatguard.llm.gemini.retry-initial-millis",
                            Long.class,
                            DEFAULT_RETRY_INITIAL_MILLIS)),
                    Duration.ofSeconds(env.getProperty(
                            "judikatguard.llm.gemini.timeout-seconds",
                            Integer.class,
                            DEFAULT_TIMEOUT_SECONDS)));
        }
    }

    /**
     * Wiring. Mirrors {@link AnthropicLlmClient.Config}: the client exists only when it has
     * been selected, so a machine with no key still boots and only the model-backed features
     * report themselves unavailable.
     */
    @Configuration(proxyBeanMethods = false)
    public static class Config {

        @Bean
        @Primary
        @Conditional(GeminiSelected.class)
        LlmClient geminiLlmClient(ObjectMapper json, Environment env) {
            Settings settings = Settings.fromEnvironment(env);
            if (settings.apiKey().isBlank()) {
                log.warn(
                        "{}={} was requested but {} is not set: the proposition check will report "
                                + "itself unavailable",
                        PROVIDER_PROPERTY,
                        PROVIDER_GEMINI,
                        API_KEY_PROPERTY);
                return new LlmClient.Unconfigured(
                        "GEMINI_API_KEY is not set, so no model call can be made");
            }
            log.info(
                    "model calls enabled via Gemini: model={} baseUrl={} thinkingLevel={} "
                            + "temperature=0 schema={} maxAttempts={}",
                    settings.model(),
                    settings.baseUrl(),
                    settings.thinkingLevel(),
                    settings.propositionSchema() ? "proposition-check.v1" : "none",
                    settings.maxAttempts());
            return new GeminiLlmClient(settings, json);
        }

        // No unavailable-fallback bean here on purpose. AnthropicLlmClient.Config already
        // declares one under the name `unavailableLlmClient`, and a second @Bean method of
        // that name would either shadow it or trip Boot's bean-definition-overriding guard.
        // With no provider selected this condition is false, nothing registers, and that
        // existing fallback stays the only LlmClient.
    }

    /**
     * The provider precedence rule, which {@code @ConditionalOnProperty} cannot express
     * because it is a disjunction. See the class javadoc for the three cases.
     */
    static final class GeminiSelected implements Condition {

        @Override
        public boolean matches(ConditionContext context, AnnotatedTypeMetadata metadata) {
            Environment env = context.getEnvironment();
            String provider = env.getProperty(
                            PROVIDER_PROPERTY, env.getProperty("jg.llm.provider", ""))
                    .strip()
                    .toLowerCase(Locale.ROOT);
            if (!provider.isEmpty()) {
                return PROVIDER_GEMINI.equals(provider);
            }
            return !env.getProperty(API_KEY_PROPERTY, "").isBlank()
                    && env.getProperty(AnthropicLlmClient.API_KEY_PROPERTY, "").isBlank();
        }
    }
}

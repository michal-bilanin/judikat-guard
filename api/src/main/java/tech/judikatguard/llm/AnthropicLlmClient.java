package tech.judikatguard.llm;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.env.Environment;
import org.springframework.http.MediaType;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestClientResponseException;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * The Anthropic Messages API over a plain {@link RestClient}. PLAN.md section 4.
 *
 * <p>No SDK and no Spring AI: one POST, one JSON body, one text reply. The interesting parts
 * of this project are the rules engine and the evidence discipline, and a hackathon does not
 * pay for a client library to reach one endpoint.
 *
 * <p>Configuration, all optional, all resolvable from the environment through Spring's
 * relaxed binding:
 *
 * <table border="1">
 *   <caption>Properties</caption>
 *   <tr><th>Property</th><th>Environment</th><th>Default</th></tr>
 *   <tr><td>{@code anthropic.api-key}</td><td>{@code ANTHROPIC_API_KEY}</td>
 *       <td>none; without it the application starts with {@link LlmClient.Unconfigured}</td></tr>
 *   <tr><td>{@code judikatguard.llm.model}</td><td>{@code JUDIKATGUARD_LLM_MODEL}</td>
 *       <td>{@value #DEFAULT_MODEL}, also reachable as {@code jg.llm.model} /
 *           {@code JG_LLM_MODEL} to match {@code pipeline/jg/config.py}</td></tr>
 *   <tr><td>{@code judikatguard.llm.base-url}</td><td>{@code JUDIKATGUARD_LLM_BASEURL}</td>
 *       <td>{@value #DEFAULT_BASE_URL}</td></tr>
 *   <tr><td>{@code judikatguard.llm.max-tokens}</td><td></td><td>{@value #DEFAULT_MAX_TOKENS}</td></tr>
 *   <tr><td>{@code judikatguard.llm.timeout-seconds}</td><td></td><td>{@value #DEFAULT_TIMEOUT_SECONDS}</td></tr>
 *   <tr><td>{@code judikatguard.llm.send-temperature}</td><td></td><td>{@code false}, see below</td></tr>
 * </table>
 *
 * <p><b>On temperature.</b> CLAUDE.md rule 7 asks for temperature 0, and the three prompt
 * files declare {@code temperature: 0}. The current models reject a non-default sampling
 * parameter outright: sending {@code "temperature": 0} to {@value #DEFAULT_MODEL} fails the
 * request with a 400. So the field is omitted by default and
 * {@code judikatguard.llm.send-temperature=true} restores it for an older model that still
 * accepts it. What the rule is actually protecting — a cached reply being a faithful replay
 * rather than a fresh sample — is preserved by {@link LlmCacheRepository}, and temperature 0
 * never guaranteed identical outputs anyway. This is a documented deviation, not an
 * oversight; see the report accompanying this change.
 */
public final class AnthropicLlmClient implements LlmClient {

    public static final String DEFAULT_MODEL = "claude-sonnet-5";

    public static final String DEFAULT_BASE_URL = "https://api.anthropic.com";

    /**
     * Generous for a one-object reply, because these models think before answering and
     * thinking tokens count against the ceiling. A reply that hits the ceiling is truncated
     * mid-JSON and reported as {@link Unavailable} rather than retried as a bad answer.
     */
    public static final int DEFAULT_MAX_TOKENS = 8192;

    public static final int DEFAULT_TIMEOUT_SECONDS = 120;

    static final String API_KEY_PROPERTY = "anthropic.api-key";

    private static final String MESSAGES_PATH = "/v1/messages";

    private static final String ANTHROPIC_VERSION = "2023-06-01";

    private static final Logger log = LoggerFactory.getLogger(AnthropicLlmClient.class);

    private final RestClient http;
    private final ObjectMapper json;
    private final String model;
    private final int maxTokens;
    private final boolean sendTemperature;

    /**
     * @param http already carrying the base URL and the authentication headers
     */
    public AnthropicLlmClient(
            RestClient http, ObjectMapper json, String model, int maxTokens, boolean sendTemperature) {
        this.http = Objects.requireNonNull(http, "http");
        this.json = Objects.requireNonNull(json, "json");
        this.model = Objects.requireNonNull(model, "model");
        this.maxTokens = maxTokens;
        this.sendTemperature = sendTemperature;
    }

    public AnthropicLlmClient(Settings settings, ObjectMapper json) {
        this(restClient(settings), json, settings.model(), settings.maxTokens(), settings.sendTemperature());
    }

    @Override
    public String model() {
        return model;
    }

    @Override
    public String complete(String prompt) {
        String body = json.writeValueAsString(requestBody(prompt));
        String raw;
        try {
            raw = http.post()
                    .uri(MESSAGES_PATH)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(body)
                    .retrieve()
                    .requiredBody(String.class);
        } catch (RestClientResponseException e) {
            // The status body carries the API's own error message, which is the difference
            // between "fix your key" and "the model is overloaded, retry the batch".
            throw new Unavailable(
                    "Anthropic Messages API returned " + e.getStatusCode() + ": "
                            + abbreviate(e.getResponseBodyAsString()), e);
        } catch (RestClientException e) {
            throw new Unavailable("Anthropic Messages API call failed: " + e.getMessage(), e);
        }
        return textOf(raw);
    }

    private Map<String, Object> requestBody(String prompt) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("model", model);
        body.put("max_tokens", maxTokens);
        if (sendTemperature) {
            body.put("temperature", 0);
        }
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("role", "user");
        message.put("content", prompt);
        body.put("messages", List.of(message));
        return body;
    }

    /**
     * Concatenates the reply's text blocks. Thinking blocks are skipped: they arrive first
     * and, at the default display setting, arrive empty.
     */
    private String textOf(String raw) {
        JsonNode root;
        try {
            root = json.readTree(raw);
        } catch (JacksonException e) {
            throw new Unavailable("Anthropic reply was not JSON: " + abbreviate(raw), e);
        }
        String stopReason = root.path("stop_reason").asString("");
        if ("refusal".equals(stopReason)) {
            throw new Unavailable(
                    "model declined the request (stop_reason=refusal, category="
                            + root.path("stop_details").path("category").asString("null") + ")");
        }
        if ("max_tokens".equals(stopReason)) {
            throw new Unavailable(
                    "reply truncated at max_tokens=" + maxTokens
                            + "; raise judikatguard.llm.max-tokens");
        }
        StringBuilder text = new StringBuilder();
        for (JsonNode block : root.path("content")) {
            if ("text".equals(block.path("type").asString(""))) {
                text.append(block.path("text").asString(""));
            }
        }
        if (text.isEmpty()) {
            throw new Unavailable("Anthropic reply carried no text block: " + abbreviate(raw));
        }
        return text.toString();
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
                .defaultHeader("x-api-key", settings.apiKey())
                .defaultHeader("anthropic-version", ANTHROPIC_VERSION)
                .build();
    }

    /** Everything the client needs to exist, resolved from configuration exactly once. */
    public record Settings(
            String baseUrl,
            String apiKey,
            String model,
            int maxTokens,
            boolean sendTemperature,
            Duration readTimeout) {

        public Settings {
            Objects.requireNonNull(baseUrl, "baseUrl");
            Objects.requireNonNull(apiKey, "apiKey");
            Objects.requireNonNull(model, "model");
            Objects.requireNonNull(readTimeout, "readTimeout");
            if (maxTokens <= 0) {
                throw new IllegalArgumentException("maxTokens must be positive, got " + maxTokens);
            }
        }

        public static Settings fromEnvironment(Environment env) {
            return new Settings(
                    env.getProperty("judikatguard.llm.base-url", DEFAULT_BASE_URL),
                    env.getProperty(API_KEY_PROPERTY, ""),
                    env.getProperty(
                            "judikatguard.llm.model",
                            env.getProperty("jg.llm.model", DEFAULT_MODEL)),
                    env.getProperty("judikatguard.llm.max-tokens", Integer.class, DEFAULT_MAX_TOKENS),
                    env.getProperty("judikatguard.llm.send-temperature", Boolean.class, false),
                    Duration.ofSeconds(env.getProperty(
                            "judikatguard.llm.timeout-seconds", Integer.class, DEFAULT_TIMEOUT_SECONDS)));
        }
    }

    /**
     * Wiring. The real client is registered only when an API key is configured, so that
     * {@code make api} works, Flyway runs and every endpoint that needs no model call answers
     * on a machine with no key; the two model-backed features then report themselves
     * unavailable instead of taking the application down at startup.
     *
     * <p>A nested static {@code @Configuration} is picked up by the application's own
     * component scan.
     */
    @Configuration(proxyBeanMethods = false)
    public static class Config {

        @Bean
        @ConditionalOnProperty(name = API_KEY_PROPERTY)
        LlmClient anthropicLlmClient(ObjectMapper json, Environment env) {
            Settings settings = Settings.fromEnvironment(env);
            if (settings.apiKey().isBlank()) {
                return unconfigured();
            }
            log.info(
                    "model calls enabled: model={} baseUrl={} maxTokens={} temperature={}",
                    settings.model(),
                    settings.baseUrl(),
                    settings.maxTokens(),
                    settings.sendTemperature() ? "0" : "omitted (rejected by current models)");
            return new AnthropicLlmClient(settings, json);
        }

        @Bean
        @ConditionalOnMissingBean(LlmClient.class)
        LlmClient unavailableLlmClient() {
            return unconfigured();
        }

        /**
         * Reads the prompt at startup on purpose: a missing or malformed
         * {@code prompts/proposition-check.v1.md} is a broken deployment and should fail
         * loudly here rather than at the first request.
         */
        @Bean
        PropositionChecker propositionChecker(
                LlmClient llm, LlmCacheRepository cache, ObjectMapper json) {
            return new PropositionChecker(
                    llm, cache, json, PromptTemplate.load(PropositionChecker.PROMPT_FILE));
        }

        private static LlmClient unconfigured() {
            log.warn(
                    "ANTHROPIC_API_KEY is not set: the proposition check and any query-time "
                            + "classification will report themselves unavailable");
            return new LlmClient.Unconfigured(
                    "ANTHROPIC_API_KEY is not set, so no model call can be made");
        }
    }
}

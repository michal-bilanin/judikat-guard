package tech.judikatguard.llm;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.hamcrest.Matchers.contains;
import static org.springframework.test.web.client.ExpectedCount.times;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.content;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.header;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.jsonPath;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.method;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withServerError;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withStatus;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.UnaryOperator;
import org.jspecify.annotations.Nullable;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.assertj.AssertableApplicationContext;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.test.web.client.MockRestServiceServer;
import org.springframework.web.client.RestClient;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * The Gemini provider, exercised entirely against a stubbed transport: no network, no API
 * key, no sleeping. What is pinned here is the wire contract (the Interactions body and the
 * {@code steps} reply shape), the defensive parsing, the free tier's 429 handling, and the
 * rule that exactly one provider wins whichever keys are set.
 *
 * <p>The evidence-span gate is not tested here. It lives above this seam, in
 * {@link PropositionChecker}, and this client is deliberately unable to affect it.
 */
class GeminiLlmClientTest {

    private static final ObjectMapper JSON = JsonMapper.builder().build();

    private static final String BASE_URL = GeminiLlmClient.DEFAULT_BASE_URL;

    private static final String INTERACTIONS = BASE_URL + GeminiLlmClient.INTERACTIONS_PATH;

    /** Never a real key; nothing in this file can reach Google. */
    private static final String TEST_KEY = "TEST-GEMINI-KEY";

    /** The reply text the model would produce for a proposition check. */
    private static final String REPLY_JSON =
            """
            {"verdict":"OVERBROAD","confidence":0.78,\
            "evidence_span":"pouze ve vztahu k věcem movitým",\
            "note":"Rozhodnutí tento závěr vyslovilo pouze ve vztahu k věcem movitým."}""";

    private static String completedResponse(String text) {
        return """
                {
                  "id": "int_123",
                  "status": "completed",
                  "steps": [
                    {"type": "user_input", "status": "done",
                     "content": [{"type": "text", "text": "IGNORED ECHO OF THE PROMPT"}]},
                    {"type": "model_output", "status": "done",
                     "content": [{"type": "text", "text": %s}]}
                  ]
                }
                """
                .formatted(JSON.writeValueAsString(text));
    }

    @Nested
    @DisplayName("the request")
    class Request {

        @Test
        @DisplayName("is the documented Interactions body, with temperature 0 nested in generation_config")
        void postsTheDocumentedBody() {
            Fixture f = Fixture.create();
            f.server
                    .expect(requestTo(INTERACTIONS))
                    .andExpect(method(HttpMethod.POST))
                    .andExpect(content().contentType(MediaType.APPLICATION_JSON))
                    // A header, not a ?key= query parameter.
                    .andExpect(header(GeminiLlmClient.API_KEY_HEADER, TEST_KEY))
                    .andExpect(jsonPath("$.model").value(GeminiLlmClient.DEFAULT_MODEL))
                    // `input` is the whole prompt as one string, not a contents/parts tree.
                    .andExpect(jsonPath("$.input").value("PROMPT"))
                    .andExpect(jsonPath("$.contents").doesNotExist())
                    // CLAUDE.md rule 7 to the letter on this provider.
                    .andExpect(jsonPath("$.generation_config.temperature").value(0))
                    .andExpect(jsonPath("$.generation_config.thinking_level").value("minimal"))
                    .andExpect(jsonPath("$.temperature").doesNotExist())
                    .andExpect(jsonPath("$.response_format.type").value("text"))
                    .andExpect(jsonPath("$.response_format.mime_type").value("application/json"))
                    .andRespond(withSuccess(completedResponse(REPLY_JSON), MediaType.APPLICATION_JSON));

            assertThat(f.client.complete("PROMPT")).isEqualTo(REPLY_JSON);
            f.server.verify();
        }

        @Test
        @DisplayName("carries the proposition schema, built from PropositionVerdict.VERDICTS")
        void constrainsTheReplyWithASchema() {
            Fixture f = Fixture.create();
            f.server
                    .expect(requestTo(INTERACTIONS))
                    .andExpect(jsonPath("$.response_format.schema.type").value("object"))
                    .andExpect(jsonPath("$.response_format.schema.properties.verdict['enum']")
                            .value(contains("CONTRADICTS", "OVERBROAD", "SUPPORTS", "UNRELATED")))
                    .andExpect(jsonPath("$.response_format.schema.properties.confidence.type")
                            .value("number"))
                    .andExpect(jsonPath("$.response_format.schema.properties.confidence.minimum").value(0))
                    .andExpect(jsonPath("$.response_format.schema.properties.confidence.maximum").value(1))
                    .andExpect(jsonPath("$.response_format.schema.properties.evidence_span.type")
                            .value("string"))
                    .andRespond(withSuccess(completedResponse(REPLY_JSON), MediaType.APPLICATION_JSON));

            f.client.complete("PROMPT");
            f.server.verify();
        }

        @Test
        @DisplayName("omits the schema, keeping the mime type, when response-schema=none")
        void schemaIsOptional() {
            Fixture f = Fixture.create(builder -> builder.schema(null));
            f.server
                    .expect(requestTo(INTERACTIONS))
                    .andExpect(jsonPath("$.response_format.mime_type").value("application/json"))
                    .andExpect(jsonPath("$.response_format.schema").doesNotExist())
                    .andRespond(withSuccess(completedResponse(REPLY_JSON), MediaType.APPLICATION_JSON));

            f.client.complete("PROMPT");
            f.server.verify();
        }

        @Test
        @DisplayName("reports the configured Gemini model id, which is what the row records")
        void modelIsTheConfiguredId() {
            assertThat(Fixture.create().client.model()).isEqualTo("gemini-3.5-flash-lite");
            assertThat(Fixture.create(b -> b.model("gemini-2.5-flash")).client.model())
                    .isEqualTo("gemini-2.5-flash");
        }
    }

    @Nested
    @DisplayName("the reply")
    class Reply {

        @Test
        @DisplayName("is the model_output step's text, concatenated across blocks")
        void concatenatesModelOutputTextBlocks() {
            Fixture f = Fixture.create();
            f.server.expect(requestTo(INTERACTIONS)).andRespond(withSuccess("""
                    {"id":"int_1","status":"completed","steps":[
                      {"type":"user_input","status":"done",
                       "content":[{"type":"text","text":"echo"}]},
                      {"type":"model_output","status":"done",
                       "content":[{"type":"text","text":"{\\"verdict\\":"},
                                  {"type":"text","text":"\\"SUPPORTS\\"}"}]}
                    ]}
                    """, MediaType.APPLICATION_JSON));

            assertThat(f.client.complete("PROMPT")).isEqualTo("{\"verdict\":\"SUPPORTS\"}");
        }

        @Test
        @DisplayName("falls back to a top-level output_text when the timeline is absent")
        void fallsBackToOutputText() {
            Fixture f = Fixture.create();
            f.server.expect(requestTo(INTERACTIONS)).andRespond(withSuccess(
                    "{\"id\":\"int_1\",\"status\":\"completed\",\"output_text\":\"" + "FALLBACK" + "\"}",
                    MediaType.APPLICATION_JSON));

            assertThat(f.client.complete("PROMPT")).isEqualTo("FALLBACK");
        }

        @Test
        @DisplayName("never becomes an empty string: an unknown shape is Unavailable with the body")
        void unknownShapeIsUnavailable() {
            Fixture f = Fixture.create();
            f.server.expect(requestTo(INTERACTIONS)).andRespond(withSuccess(
                    "{\"id\":\"int_1\",\"status\":\"failed\",\"candidates\":[{\"text\":\"nope\"}]}",
                    MediaType.APPLICATION_JSON));

            assertThatThrownBy(() -> f.client.complete("PROMPT"))
                    .isInstanceOf(LlmClient.Unavailable.class)
                    .hasMessageContaining("no model_output text")
                    .hasMessageContaining("status=failed")
                    .hasMessageContaining("candidates");
        }

        @Test
        @DisplayName("a model_output step with no text block is Unavailable, not empty")
        void emptyModelOutputIsUnavailable() {
            Fixture f = Fixture.create();
            f.server.expect(requestTo(INTERACTIONS)).andRespond(withSuccess(
                    "{\"status\":\"completed\",\"steps\":[{\"type\":\"model_output\","
                            + "\"content\":[{\"type\":\"thought\"}]}]}",
                    MediaType.APPLICATION_JSON));

            assertThatThrownBy(() -> f.client.complete("PROMPT"))
                    .isInstanceOf(LlmClient.Unavailable.class)
                    .hasMessageContaining("no model_output text");
        }

        @Test
        @DisplayName("a non-JSON body is Unavailable carrying a truncated copy of it")
        void nonJsonIsUnavailable() {
            Fixture f = Fixture.create();
            f.server.expect(requestTo(INTERACTIONS))
                    .andRespond(withSuccess("<html>502 Bad Gateway</html>", MediaType.TEXT_HTML));

            assertThatThrownBy(() -> f.client.complete("PROMPT"))
                    .isInstanceOf(LlmClient.Unavailable.class)
                    .hasMessageContaining("was not JSON")
                    .hasMessageContaining("502 Bad Gateway");
        }
    }

    @Nested
    @DisplayName("the free tier's rate limits")
    class RateLimits {

        @Test
        @DisplayName("a 429 is retried, honouring Retry-After, without sleeping in the test")
        void retriesA429HonouringRetryAfter() {
            Fixture f = Fixture.create();
            f.server
                    .expect(requestTo(INTERACTIONS))
                    .andRespond(withStatus(HttpStatus.TOO_MANY_REQUESTS)
                            .header("Retry-After", "7")
                            .body("{\"error\":{\"code\":429,\"status\":\"RESOURCE_EXHAUSTED\"}}")
                            .contentType(MediaType.APPLICATION_JSON));
            f.server
                    .expect(requestTo(INTERACTIONS))
                    .andRespond(withSuccess(completedResponse(REPLY_JSON), MediaType.APPLICATION_JSON));

            assertThat(f.client.complete("PROMPT")).isEqualTo(REPLY_JSON);
            assertThat(f.waits).containsExactly(Duration.ofSeconds(7));
            f.server.verify();
        }

        @Test
        @DisplayName("a 5xx without Retry-After backs off exponentially with jitter")
        void backsOffWhenNoRetryAfterHeader() {
            Fixture f = Fixture.create(b -> b.retryInitial(Duration.ofMillis(400)));
            f.server.expect(requestTo(INTERACTIONS)).andRespond(withServerError());
            f.server.expect(requestTo(INTERACTIONS)).andRespond(withServerError());
            f.server
                    .expect(requestTo(INTERACTIONS))
                    .andRespond(withSuccess(completedResponse(REPLY_JSON), MediaType.APPLICATION_JSON));

            assertThat(f.client.complete("PROMPT")).isEqualTo(REPLY_JSON);
            assertThat(f.waits).hasSize(2);
            assertThat(f.waits.get(0)).isBetween(Duration.ofMillis(200), Duration.ofMillis(400));
            assertThat(f.waits.get(1)).isBetween(Duration.ofMillis(400), Duration.ofMillis(800));
        }

        @Test
        @DisplayName("retries are bounded and the final error names the status and the count")
        void givesUpAfterMaxAttempts() {
            Fixture f = Fixture.create(b -> b.maxAttempts(3));
            f.server
                    .expect(times(3), requestTo(INTERACTIONS))
                    .andRespond(withStatus(HttpStatus.TOO_MANY_REQUESTS)
                            .body("{\"error\":{\"status\":\"RESOURCE_EXHAUSTED\"}}")
                            .contentType(MediaType.APPLICATION_JSON));

            assertThatThrownBy(() -> f.client.complete("PROMPT"))
                    .isInstanceOf(LlmClient.Unavailable.class)
                    .hasMessageContaining("429")
                    .hasMessageContaining("3 attempt(s)")
                    .hasMessageContaining("RESOURCE_EXHAUSTED");
            assertThat(f.waits).hasSize(2);
            f.server.verify();
        }

        @Test
        @DisplayName("a 400 is not retried: repeating a rejected schema only burns the daily quota")
        void doesNotRetryClientErrors() {
            Fixture f = Fixture.create();
            f.server
                    .expect(times(1), requestTo(INTERACTIONS))
                    .andRespond(withStatus(HttpStatus.BAD_REQUEST)
                            .body("{\"error\":{\"message\":\"Invalid JSON payload\"}}")
                            .contentType(MediaType.APPLICATION_JSON));

            assertThatThrownBy(() -> f.client.complete("PROMPT"))
                    .isInstanceOf(LlmClient.Unavailable.class)
                    .hasMessageContaining("400")
                    .hasMessageContaining("Invalid JSON payload");
            assertThat(f.waits).isEmpty();
            f.server.verify();
        }

        @Test
        @DisplayName("an absurd Retry-After is ignored in favour of the bounded backoff")
        void ignoresAnAbsurdRetryAfter() {
            Fixture f = Fixture.create(b -> b.retryInitial(Duration.ofMillis(400)));
            f.server
                    .expect(requestTo(INTERACTIONS))
                    .andRespond(withStatus(HttpStatus.TOO_MANY_REQUESTS).header("Retry-After", "86400"));
            f.server
                    .expect(requestTo(INTERACTIONS))
                    .andRespond(withSuccess(completedResponse(REPLY_JSON), MediaType.APPLICATION_JSON));

            f.client.complete("PROMPT");
            assertThat(f.waits).hasSize(1);
            assertThat(f.waits.getFirst()).isLessThanOrEqualTo(Duration.ofMillis(400));
        }
    }

    @Nested
    @DisplayName("settings")
    class SettingsFromEnvironment {

        @Test
        @DisplayName("default to the free-tier model and the documented endpoint")
        void defaults() {
            GeminiLlmClient.Settings s =
                    GeminiLlmClient.Settings.fromEnvironment(new MockEnvironment());
            assertThat(s.model()).isEqualTo(GeminiLlmClient.DEFAULT_MODEL);
            assertThat(s.baseUrl()).isEqualTo(GeminiLlmClient.DEFAULT_BASE_URL);
            assertThat(s.thinkingLevel()).isEqualTo("minimal");
            assertThat(s.propositionSchema()).isTrue();
            assertThat(s.maxAttempts()).isEqualTo(GeminiLlmClient.DEFAULT_MAX_ATTEMPTS);
            assertThat(s.apiKey()).isEmpty();
        }

        @Test
        @DisplayName("are all overridable, including the model id")
        void overrides() {
            MockEnvironment env = new MockEnvironment()
                    .withProperty("gemini.api-key", TEST_KEY)
                    .withProperty("judikatguard.llm.gemini.model", "gemini-3.8-flash")
                    .withProperty("judikatguard.llm.gemini.thinking-level", "low")
                    .withProperty("judikatguard.llm.gemini.response-schema", "none")
                    .withProperty("judikatguard.llm.gemini.max-attempts", "2")
                    .withProperty("judikatguard.llm.gemini.retry-initial-millis", "250")
                    .withProperty("judikatguard.llm.gemini.timeout-seconds", "30");

            GeminiLlmClient.Settings s = GeminiLlmClient.Settings.fromEnvironment(env);
            assertThat(s.model()).isEqualTo("gemini-3.8-flash");
            assertThat(s.thinkingLevel()).isEqualTo("low");
            assertThat(s.propositionSchema()).isFalse();
            assertThat(s.maxAttempts()).isEqualTo(2);
            assertThat(s.retryInitial()).isEqualTo(Duration.ofMillis(250));
            assertThat(s.readTimeout()).isEqualTo(Duration.ofSeconds(30));
        }
    }

    /**
     * Exactly one provider must win, and which one must not depend on bean-processing order.
     * Both keys are always set explicitly, blank where "absent" is meant, so that a real
     * {@code ANTHROPIC_API_KEY} or {@code GEMINI_API_KEY} in the developer's environment
     * cannot change the outcome.
     */
    @Nested
    @DisplayName("provider selection")
    class ProviderSelection {

        private final ApplicationContextRunner runner = new ApplicationContextRunner()
                .withUserConfiguration(GeminiLlmClient.Config.class, AnthropicLlmClient.Config.class)
                .withBean(ObjectMapper.class, () -> JsonMapper.builder().build())
                .withBean(LlmCacheRepository.class, NoCache::new);

        private ApplicationContextRunner with(String gemini, String anthropic, String... more) {
            List<String> properties = new ArrayList<>(
                    List.of("gemini.api-key=" + gemini, "anthropic.api-key=" + anthropic));
            properties.addAll(List.of(more));
            return runner.withPropertyValues(properties.toArray(String[]::new));
        }

        @Test
        @DisplayName("a Gemini key alone gives the Gemini client")
        void geminiKeyAlone() {
            with(TEST_KEY, "").run(this::assertsGemini);
        }

        @Test
        @DisplayName("an Anthropic key alone leaves the Anthropic client untouched")
        void anthropicKeyAlone() {
            with("", "TEST-ANTHROPIC-KEY")
                    .run(ctx -> assertThat(ctx.getBean(LlmClient.class))
                            .isInstanceOf(AnthropicLlmClient.class));
        }

        @Test
        @DisplayName("both keys and no explicit provider keeps the pre-existing Anthropic behaviour")
        void bothKeysDefaultToAnthropic() {
            with(TEST_KEY, "TEST-ANTHROPIC-KEY")
                    .run(ctx -> assertThat(ctx.getBean(LlmClient.class))
                            .isInstanceOf(AnthropicLlmClient.class));
        }

        @Test
        @DisplayName("both keys and provider=gemini resolves to Gemini")
        void explicitProviderWins() {
            with(TEST_KEY, "TEST-ANTHROPIC-KEY", "judikatguard.llm.provider=gemini")
                    .run(this::assertsGemini);
        }

        @Test
        @DisplayName("provider=anthropic leaves the Gemini client unregistered")
        void explicitAnthropic() {
            with(TEST_KEY, "TEST-ANTHROPIC-KEY", "judikatguard.llm.provider=anthropic")
                    .run(ctx -> assertThat(ctx.getBean(LlmClient.class))
                            .isInstanceOf(AnthropicLlmClient.class));
        }

        @Test
        @DisplayName("neither key still boots and reports itself unavailable")
        void neitherKey() {
            with("", "").run(ctx -> {
                assertThat(ctx).hasNotFailed();
                assertThat(ctx.getBean(LlmClient.class)).isInstanceOf(LlmClient.Unconfigured.class);
                assertThat(ctx.getBean(LlmClient.class).model()).isEqualTo("unavailable");
            });
        }

        @Test
        @DisplayName("provider=gemini without a key reports unavailable rather than silently "
                + "falling back")
        void explicitGeminiWithoutAKey() {
            with("", "TEST-ANTHROPIC-KEY", "judikatguard.llm.provider=gemini").run(ctx -> {
                assertThat(ctx).hasNotFailed();
                assertThat(ctx.getBean(LlmClient.class)).isInstanceOf(LlmClient.Unconfigured.class);
            });
        }

        private void assertsGemini(AssertableApplicationContext ctx) {
            assertThat(ctx).hasNotFailed();
            LlmClient llm = ctx.getBean(LlmClient.class);
            assertThat(llm).isInstanceOf(GeminiLlmClient.class);
            assertThat(llm.model()).isEqualTo(GeminiLlmClient.DEFAULT_MODEL);
        }
    }

    /** The proposition checker needs one; nothing in this file exercises it. */
    private static final class NoCache implements LlmCacheRepository {

        @Override
        public Optional<CachedCall> find(String cacheKey) {
            return Optional.empty();
        }

        @Override
        public void put(CachedCall call) {
            throw new UnsupportedOperationException();
        }
    }

    /** A client wired to a {@link MockRestServiceServer} and a pause that records rather than sleeps. */
    private record Fixture(GeminiLlmClient client, MockRestServiceServer server, List<Duration> waits) {

        static Fixture create() {
            return create(builder -> builder);
        }

        static Fixture create(UnaryOperator<Builder> customise) {
            Builder b = customise.apply(new Builder());
            RestClient.Builder http = RestClient.builder()
                    .baseUrl(BASE_URL)
                    .defaultHeader(GeminiLlmClient.API_KEY_HEADER, TEST_KEY);
            MockRestServiceServer server = MockRestServiceServer.bindTo(http).build();
            List<Duration> waits = new ArrayList<>();
            return new Fixture(
                    new GeminiLlmClient(
                            http.build(),
                            JSON,
                            b.model,
                            "minimal",
                            b.schema,
                            b.maxAttempts,
                            b.retryInitial,
                            waits::add),
                    server,
                    waits);
        }
    }

    private static final class Builder {

        private String model = GeminiLlmClient.DEFAULT_MODEL;
        private @Nullable Map<String, Object> schema =
                GeminiLlmClient.propositionResponseSchema();
        private int maxAttempts = GeminiLlmClient.DEFAULT_MAX_ATTEMPTS;
        private Duration retryInitial = Duration.ofMillis(GeminiLlmClient.DEFAULT_RETRY_INITIAL_MILLIS);

        Builder model(String value) {
            this.model = value;
            return this;
        }

        Builder schema(@Nullable Map<String, Object> value) {
            this.schema = value;
            return this;
        }

        Builder maxAttempts(int value) {
            this.maxAttempts = value;
            return this;
        }

        Builder retryInitial(Duration value) {
            this.retryInitial = value;
            return this;
        }
    }
}

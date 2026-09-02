package tech.judikatguard.decision;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.core.env.Environment;
import org.springframework.jdbc.core.simple.JdbcClient;
import tech.judikatguard.document.DocumentChecker;
import tech.judikatguard.document.PropositionService;
import tech.judikatguard.document.StatusService;
import tech.judikatguard.extract.CitationExtractor;
import tech.judikatguard.llm.LlmClient;
import tech.judikatguard.web.EvaluationContextFilter;

/**
 * The application starts with no model credentials.
 *
 * <p>This is a deliberate property, not an accident of configuration. Everything except the
 * proposition check is a database read, so a machine with no {@code ANTHROPIC_API_KEY} must
 * still boot, migrate and serve: {@code make api} on a fresh clone has to work, and the two
 * model-backed features report themselves unavailable instead of taking the process down.
 */
class ApplicationContextTest extends PostgresBackedTest {

    @Autowired Environment environment;
    @Autowired LlmClient llm;
    @Autowired JdbcClient jdbc;
    @Autowired CitationExtractor extractor;
    @Autowired DocumentChecker checker;
    @Autowired StatusService statuses;
    @Autowired PropositionService propositions;
    @Autowired EvaluationContextFilter filter;

    @Test
    @DisplayName("the read path is wired and Flyway has applied every migration")
    void contextLoads() {
        assertThat(checker).isNotNull();
        assertThat(statuses).isNotNull();
        assertThat(propositions).isNotNull();
        assertThat(filter).isNotNull();
        assertThat(extractor.patterns().patterns())
                .as("extract/patterns.toml is loaded at startup, not at first request")
                .isNotEmpty();

        Integer applied = jdbc
                .sql("select count(*) from flyway_schema_history where success = true")
                .query(Integer.class)
                .single();
        assertThat(applied).isGreaterThanOrEqualTo(4);
        assertThat(jdbc.sql("select count(*) from departure_authority")
                        .query(Integer.class)
                        .single())
                .as("V2 seeds the legal authority rules; the join depends on them")
                .isEqualTo(9);
    }

    @Test
    @DisplayName("without an API key the LlmClient is the unconfigured fallback")
    void llmDegradesInsteadOfFailing() {
        String key = environment.getProperty("anthropic.api-key");
        assumeTrue(
                key == null || key.isBlank(),
                "ANTHROPIC_API_KEY is set in this environment, so the real client is wired");

        assertThat(llm).isInstanceOf(LlmClient.Unconfigured.class);
        assertThat(llm.model()).isEqualTo("unavailable");
    }
}

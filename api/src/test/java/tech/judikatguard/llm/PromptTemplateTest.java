package tech.judikatguard.llm;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

/**
 * Prompt loading and rendering. Two things are being protected here.
 *
 * <p>{@link PromptTemplate#version()} must be exactly the string in the file, because every
 * {@code treatment}, {@code provision_materiality} and {@code llm_cache} row records it and a
 * drifted version detaches stored labels from the prompt that produced them (CLAUDE.md
 * rule 8).
 *
 * <p>{@link PromptTemplate#render} must never produce a half-substituted prompt. A prompt
 * with a literal {@code {{context}}} still gets a confident, well-formed, worthless answer
 * back, and nothing downstream can tell.
 *
 * <p>The real files in {@code prompts/} are the fixtures: no database and no API key needed,
 * and the parity between frontmatter and body gets checked on every build.
 */
class PromptTemplateTest {

    @Nested
    @DisplayName("the repository's own prompts")
    class RealPrompts {

        @ParameterizedTest(name = "{0}")
        @ValueSource(strings = {
            "treatment-classify.v1.md",
            "proposition-check.v1.md",
            "provision-materiality.v1.md"
        })
        @DisplayName("load, and their version is the file name without .md")
        void loadWithTheDeclaredVersion(String fileName) {
            PromptTemplate template = PromptTemplate.load(fileName);

            assertThat(template.name()).isEqualTo(fileName);
            assertThat(template.version()).isEqualTo(fileName.replace(".md", ""));
            assertThat(template.cacheKey()).isNotBlank();
            assertThat(template.placeholders()).isNotEmpty();
        }

        @ParameterizedTest(name = "{0}")
        @ValueSource(strings = {
            "treatment-classify.v1.md",
            "proposition-check.v1.md",
            "provision-materiality.v1.md"
        })
        @DisplayName("keep neither frontmatter nor implementer comments in the body")
        void stripFrontmatterAndComments(String fileName) {
            String body = PromptTemplate.load(fileName).body();

            assertThat(body).doesNotStartWith("---");
            assertThat(body).doesNotContain("version:");
            assertThat(body).doesNotContain("<!--");
            assertThat(body).doesNotContain("FROZEN");
        }

        @Test
        @DisplayName("proposition-check.v1 renders with the six values the checker supplies")
        void propositionCheckRenders() {
            PromptTemplate template = PromptTemplate.load("proposition-check.v1.md");

            assertThat(template.placeholders())
                    .containsExactly(
                            "cited_context", "cited_court", "cited_date", "cited_ecli",
                            "cited_ratio", "claim");

            // PLAN.md section 11 uses exactly this decision as its worked example.
            String rendered = template.render(Map.of(
                    "cited_ecli", "ECLI:CZ:NSS:2015:6.Ads.45.2014.32",
                    "cited_court", "NSS",
                    "cited_date", "2015-03-12",
                    "cited_ratio", "Závěr se uplatní jen u věcí movitých.",
                    "cited_context", "Soud uzavřel, že jde o věci movité.",
                    "claim", "Rozhodnutí platí pro všechny věci."));

            assertThat(rendered)
                    .doesNotContain("{{")
                    .contains("ECLI:CZ:NSS:2015:6.Ads.45.2014.32")
                    .contains("Závěr se uplatní jen u věcí movitých.")
                    .contains("Rozhodnutí platí pro všechny věci.");
        }
    }

    @Nested
    @DisplayName("parsing")
    class Parsing {

        @Test
        void readsVersionAndCacheKeyFromTheFrontmatter() {
            PromptTemplate template = PromptTemplate.parse("TEST-prompt.v1.md", """
                    ---
                    version: TEST-prompt.v1
                    cache_key: (a, b, prompt_version)
                    temperature: 0
                    placeholders:
                      - a
                    ---

                    Body with {{a}}.
                    """);

            assertThat(template.version()).isEqualTo("TEST-prompt.v1");
            assertThat(template.cacheKey()).isEqualTo("(a, b, prompt_version)");
            assertThat(template.body()).isEqualTo("Body with {{a}}.");
            assertThat(template.placeholders()).containsExactly("a");
        }

        @Test
        @DisplayName("a placeholder mentioned only inside a comment is not a placeholder")
        void ignoresPlaceholdersInComments() {
            PromptTemplate template = PromptTemplate.parse("TEST-prompt.v1.md", """
                    ---
                    version: TEST-prompt.v1
                    placeholders:
                      - a
                    ---

                    <!--
                    {{b}} is discussed here but never substituted.
                    -->

                    Body with {{a}}.
                    """);

            assertThat(template.placeholders()).containsExactly("a");
        }

        @Test
        void rejectsAFileWithoutFrontmatter() {
            assertThatThrownBy(() -> PromptTemplate.parse("TEST-prompt.v1.md", "Body with {{a}}."))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("frontmatter");
        }

        @Test
        void rejectsFrontmatterWithoutAVersion() {
            assertThatThrownBy(() -> PromptTemplate.parse("TEST-prompt.v1.md", """
                    ---
                    cache_key: (a, prompt_version)
                    ---

                    Body with {{a}}.
                    """))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("version");
        }

        @Test
        @DisplayName("rejects frontmatter that lies about the body's placeholders")
        void rejectsADeclarationMismatch() {
            assertThatThrownBy(() -> PromptTemplate.parse("TEST-prompt.v1.md", """
                    ---
                    version: TEST-prompt.v1
                    placeholders:
                      - a
                      - b
                    ---

                    Body with {{a}}.
                    """))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("[a, b]");
        }

        @Test
        void rejectsAnUnterminatedFrontmatter() {
            assertThatThrownBy(() -> PromptTemplate.parse("TEST-prompt.v1.md", """
                    ---
                    version: TEST-prompt.v1
                    """))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("unterminated");
        }
    }

    @Nested
    @DisplayName("rendering")
    class Rendering {

        private static final PromptTemplate TEMPLATE = PromptTemplate.parse("TEST-prompt.v1.md", """
                ---
                version: TEST-prompt.v1
                placeholders:
                  - court
                  - claim
                ---

                Court {{court}} was cited for: {{claim}}. Again: {{claim}}.
                """);

        @Test
        void substitutesEveryOccurrence() {
            String rendered = TEMPLATE.render(Map.of("court", "NSS", "claim", "vše"));

            assertThat(rendered).isEqualTo("Court NSS was cited for: vše. Again: vše.");
        }

        @Test
        @DisplayName("a missing value fails loudly rather than rendering half a prompt")
        void rejectsAMissingValue() {
            assertThatThrownBy(() -> TEMPLATE.render(Map.of("court", "NSS")))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("unsubstituted placeholders [claim]");
        }

        @Test
        @DisplayName("an unknown key is a caller bug, most likely a renamed placeholder")
        void rejectsAnUnknownKey() {
            Map<String, String> values = new LinkedHashMap<>();
            values.put("court", "NSS");
            values.put("claim", "vše");
            values.put("citing_court", "US");

            assertThatThrownBy(() -> TEMPLATE.render(values))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("[citing_court]");
        }

        @Test
        void acceptsAnEmptyValue() {
            // provision.subsec is nullable, so "§ 2000 with no odst." is a legitimate render.
            assertThat(TEMPLATE.render(Map.of("court", "", "claim", "vše")))
                    .isEqualTo("Court  was cited for: vše. Again: vše.");
        }

        @Test
        @DisplayName("a value that itself looks like a placeholder is inserted literally")
        void doesNotRescanSubstitutedText() {
            String rendered = TEMPLATE.render(Map.of("court", "{{claim}}", "claim", "vše"));

            assertThat(rendered).isEqualTo("Court {{claim}} was cited for: vše. Again: vše.");
        }

        @Test
        @DisplayName("a value containing $ or \\ survives, since quotations contain both")
        void insertsRegexReplacementCharactersLiterally() {
            String rendered = TEMPLATE.render(Map.of("court", "NSS", "claim", "a $1 \\ b"));

            assertThat(rendered).isEqualTo("Court NSS was cited for: a $1 \\ b. Again: a $1 \\ b.");
        }
    }

    @Nested
    @DisplayName("locating prompts/")
    class Locating {

        @Test
        @DisplayName("the search finds the shared directory from api/, where Maven runs")
        void findsTheSharedDirectory() {
            Path dir = PromptTemplate.promptsDir();

            assertThat(Files.isDirectory(dir)).isTrue();
            assertThat(Files.isRegularFile(dir.resolve("proposition-check.v1.md"))).isTrue();
        }
    }
}

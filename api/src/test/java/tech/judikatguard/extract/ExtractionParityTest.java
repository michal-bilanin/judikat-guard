package tech.judikatguard.extract;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.assertj.core.api.SoftAssertions;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.jspecify.annotations.Nullable;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Cross-runtime parity. PLAN.md section 7: extraction runs in Python over the crawled corpus
 * and in Java over documents uploaded at query time, and
 * {@code eval/extraction-golden.jsonl} pins the output both must reproduce. Divergence fails
 * the build — that fixture is the only thing standing between two implementations of the
 * same regexes and a slow drift nobody notices until a citation goes missing from a report.
 *
 * <p>The fixture is loaded from disk rather than from the classpath because it is shared with
 * the Python runtime and lives at the repo root, next to {@code extract/patterns.toml}. If it
 * is absent or empty these tests <em>skip</em> with an explicit message: a silent pass would
 * be worse than no test at all.
 *
 * <p>Fixture conventions, documented in {@code eval/README.md} and asserted below:
 * {@code start} / {@code end} are offsets into {@code text}, {@code text[start:end]} equals
 * {@code raw_text}, {@code raw_text} is the whole match including its prefix, a capture group
 * that did not participate is omitted rather than null, and {@code expected} is sorted by
 * {@code start}.
 */
class ExtractionParityTest {

    /** Override for the fixture path, so the test does not depend on the working directory. */
    static final String GOLDEN_PROPERTY = "jg.golden.jsonl";

    static final String GOLDEN_RELATIVE = "eval/extraction-golden.jsonl";

    /** Every fixture is a single paragraph, so its offsets are paragraph offsets. */
    private static final int PARAGRAPH_IDX = 1;

    private static final String SKIP_MESSAGE =
            GOLDEN_RELATIVE
                    + " not found above "
                    + Path.of("").toAbsolutePath()
                    + " (override with -D"
                    + GOLDEN_PROPERTY
                    + "=/path/to/extraction-golden.jsonl). Cross-runtime extraction parity is"
                    + " UNVERIFIED for this run.";

    // ------------------------------------------------------------------ the tests

    @Test
    @DisplayName("the Java extractor reproduces eval/extraction-golden.jsonl exactly")
    void parityWithThePythonExtractor() {
        List<GoldenCase> cases = goldenCases();
        CitationExtractor extractor = CitationExtractor.withSharedPatterns();

        SoftAssertions softly = new SoftAssertions();
        for (GoldenCase golden : cases) {
            List<Reference> actual = extractor.extractParagraph(golden.text(), PARAGRAPH_IDX);

            softly.assertThat(render(actual))
                    .as("fixture '%s': %s", golden.id(), golden.note())
                    .containsExactlyElementsOf(renderExpected(golden.expected()));
        }
        softly.assertAll();
    }

    @Test
    @DisplayName("extract(document) agrees with extractParagraph on a one-paragraph fixture")
    void documentLevelExtractionAgrees() {
        List<GoldenCase> cases = goldenCases();
        CitationExtractor extractor = CitationExtractor.withSharedPatterns();

        SoftAssertions softly = new SoftAssertions();
        int checked = 0;
        for (GoldenCase golden : cases) {
            List<String> paragraphs = Paragraphs.split(golden.text());
            if (paragraphs.size() != 1 || !paragraphs.getFirst().equals(golden.text())) {
                // A fixture that splits into several paragraphs has offsets into the whole
                // text, which is not what extract(document) reports. Left to the parity test.
                continue;
            }
            checked++;
            softly.assertThat(render(extractor.extract(golden.text())))
                    .as("fixture '%s' through the document-level entry point", golden.id())
                    .containsExactlyElementsOf(renderExpected(golden.expected()));
        }
        softly.assertAll();
        assertThat(checked)
                .as("no single-paragraph fixture to check extract(document) against")
                .isPositive();
    }

    @Test
    @DisplayName("the fixture is self-consistent: verbatim spans, sorted, already normalised")
    void fixtureIsSelfConsistent() {
        List<GoldenCase> cases = goldenCases();

        SoftAssertions softly = new SoftAssertions();
        for (GoldenCase golden : cases) {
            String text = golden.text();

            // Production normalises a paragraph before extracting. A fixture text that is not
            // already normalised would pin offsets no runtime ever computes.
            softly.assertThat(Paragraphs.normalise(text))
                    .as("fixture '%s' text must already be whitespace-normalised", golden.id())
                    .isEqualTo(text);
            // BMP only, so Python code-point offsets and Java UTF-16 offsets agree.
            softly.assertThat(text.codePoints().allMatch(codePoint -> codePoint <= 0xFFFF))
                    .as("fixture '%s' must stay inside the BMP", golden.id())
                    .isTrue();

            int previousStart = -1;
            for (Expected expected : golden.expected()) {
                softly.assertThat(expected.end())
                        .as("fixture '%s' span %s must lie inside the text", golden.id(), expected.rawText())
                        .isLessThanOrEqualTo(text.length());
                if (expected.end() <= text.length()) {
                    softly.assertThat(text.substring(expected.start(), expected.end()))
                            .as("fixture '%s' offsets must be verbatim", golden.id())
                            .isEqualTo(expected.rawText());
                }
                softly.assertThat(expected.start())
                        .as("fixture '%s' expected list must be sorted by start", golden.id())
                        .isGreaterThanOrEqualTo(previousStart);
                previousStart = expected.start();
                softly.assertThat(expected.groups().values())
                        .as("fixture '%s': a group that did not participate is omitted, never null", golden.id())
                        .doesNotContainNull();
            }
        }
        softly.assertAll();
    }

    // ------------------------------------------------------------------ fixture loading

    /** One line of the fixture. {@code note} is documentation; implementations ignore it. */
    record GoldenCase(String id, String note, String text, List<Expected> expected) {}

    /** One expected reference. {@code kind} is the wire form, e.g. {@code ref_no}. */
    record Expected(String kind, String rawText, int start, int end, Map<String, String> groups) {}

    /** The fixture, or a skipped test when it is absent or empty. */
    private static List<GoldenCase> goldenCases() {
        Path path = locate();
        assumeTrue(path != null, SKIP_MESSAGE);
        List<GoldenCase> cases = load(path);
        assumeTrue(
                !cases.isEmpty(),
                path + " has no fixture lines. Cross-runtime extraction parity is UNVERIFIED.");
        return cases;
    }

    /** The property override, else the nearest {@value #GOLDEN_RELATIVE} at or above the cwd. */
    static @Nullable Path locate() {
        String override = System.getProperty(GOLDEN_PROPERTY);
        if (override != null && !override.isBlank()) {
            Path path = Path.of(override).toAbsolutePath().normalize();
            return Files.isRegularFile(path) ? path : null;
        }
        Path directory = Path.of("").toAbsolutePath().normalize();
        while (directory != null) {
            Path candidate = directory.resolve(GOLDEN_RELATIVE);
            if (Files.isRegularFile(candidate)) {
                return candidate;
            }
            directory = directory.getParent();
        }
        return null;
    }

    static List<GoldenCase> load(Path path) {
        JsonMapper mapper = JsonMapper.builder().build();
        List<GoldenCase> cases = new ArrayList<>();
        List<String> lines;
        try {
            lines = Files.readAllLines(path, StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new UncheckedIOException("cannot read " + path, e);
        }
        for (String line : lines) {
            if (line.isBlank() || line.stripLeading().startsWith("#")) {
                continue;
            }
            JsonNode node = mapper.readTree(line);
            cases.add(
                    new GoldenCase(
                            text(node, "id"),
                            text(node, "note"),
                            text(node, "text"),
                            expected(node.path("expected"))));
        }
        return cases;
    }

    private static List<Expected> expected(JsonNode array) {
        List<Expected> expected = new ArrayList<>();
        for (JsonNode node : array) {
            expected.add(
                    new Expected(
                            text(node, "kind"),
                            text(node, "raw_text"),
                            node.path("start").asInt(),
                            node.path("end").asInt(),
                            groups(node.path("groups"))));
        }
        return expected;
    }

    private static Map<String, String> groups(JsonNode node) {
        Map<String, String> groups = new TreeMap<>();
        for (String name : node.propertyNames()) {
            groups.put(name, text(node, name));
        }
        return groups;
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.path(field);
        return value.isMissingNode() || value.isNull() ? "" : value.asString();
    }

    // ------------------------------------------------------------------ comparison

    /**
     * Canonical one-line form of a reference: kind, offsets, the verbatim match and the
     * groups. Comparing rendered strings rather than objects is what makes a divergence
     * readable in the failure output, which matters because whoever sees this test fail is
     * looking at two runtimes and needs to know which field moved.
     */
    private static List<String> render(List<Reference> references) {
        return references.stream()
                .map(
                        reference ->
                                canonical(
                                        reference.kind().wireName(),
                                        reference.start(),
                                        reference.end(),
                                        reference.rawText(),
                                        reference.groups()))
                .toList();
    }

    private static List<String> renderExpected(List<Expected> expected) {
        return expected.stream()
                .map(
                        one ->
                                canonical(
                                        one.kind(), one.start(), one.end(), one.rawText(), one.groups()))
                .toList();
    }

    private static String canonical(
            String kind, int start, int end, String rawText, Map<String, String> groups) {
        return kind + " [" + start + "," + end + ") '" + rawText + "' " + new TreeMap<>(groups);
    }
}

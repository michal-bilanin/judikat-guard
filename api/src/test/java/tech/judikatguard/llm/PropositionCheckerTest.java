package tech.judikatguard.llm;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.LocalDate;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

/**
 * The proposition check, driven by a stub model and a stub cache: no API key, no database,
 * no network. PLAN.md section 12.
 *
 * <p>What is actually under test is the discipline around the model, not the model: exactly
 * one retry, the violation stated back, {@code UNCLASSIFIED} on a second failure, nothing
 * stored that failed validation, and a cache hit replacing the call entirely.
 *
 * <p>The decision identifier is the one PLAN.md section 11 uses as its worked example. The
 * Czech sentences are prose. The stub model reports itself as {@code TEST-model}.
 */
class PropositionCheckerTest {

    /** U+00A0, written as an escape so that no editor can silently normalise it. */
    private static final String NBSP = "\u00A0";

    private static final String CITED_ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32";
    private static final String CITED_COURT = "NSS";
    private static final LocalDate CITED_DATE = LocalDate.parse("2015-03-12");
    private static final String PROMPT_VERSION = "proposition-check.v1";

    private static final String RATIO = "Závěr o odpovědnosti se uplatní jen u věcí movitých.";

    /** The line break in the middle of the quotable sentence is the point of this fixture. */
    private static final String CONTEXT = """
            Nejvyšší správní soud předesílá, že závěry, k nimž dospěl,
            se vztahují pouze k věcem movitým.

            Na nemovité věci je proto bez dalšího vztáhnout nelze.""";

    private static final String CLAIM = "Rozhodnutí platí pro všechny věci bez rozdílu.";

    /** Verbatim in {@link #CONTEXT}, apart from the line break. */
    private static final String GOOD_SPAN = "závěry, k nimž dospěl, se vztahují pouze k věcem movitým";

    private static final String NOTE =
            "Rozhodnutí tento závěr vyslovilo pouze ve vztahu k věcem movitým.";

    // ---------------------------------------------------------------- happy path

    @Nested
    @DisplayName("a valid reply")
    class Valid {

        @Test
        void becomesAVerdictAfterOneCall() {
            StubLlm llm = new StubLlm(reply("OVERBROAD", "0.78", GOOD_SPAN, NOTE));
            StubCache cache = new StubCache();

            PropositionVerdict verdict = check(llm, cache);

            assertThat(verdict.verdict()).isEqualTo(PropositionVerdict.OVERBROAD);
            assertThat(verdict.confidence()).isEqualTo(0.78);
            assertThat(verdict.evidenceSpan()).isEqualTo(GOOD_SPAN);
            assertThat(verdict.note()).isEqualTo(NOTE);
            assertThat(verdict.isUnclassified()).isFalse();
            assertThat(llm.prompts).hasSize(1);
        }

        @Test
        @DisplayName("is shown the holding, the passages and the claim")
        void wasGivenTheMaterialItMustQuote() {
            StubLlm llm = new StubLlm(reply("SUPPORTS", "0.9", GOOD_SPAN, NOTE));

            check(llm, new StubCache());

            assertThat(llm.prompts.getFirst())
                    .contains(CITED_ECLI)
                    .contains(CITED_COURT)
                    .contains("2015-03-12")
                    .contains(RATIO)
                    .contains(CLAIM)
                    .doesNotContain("{{");
        }

        @Test
        @DisplayName("is cached with the model and the prompt version that produced it")
        void isCachedWithItsProvenance() {
            StubCache cache = new StubCache();

            check(new StubLlm(reply("OVERBROAD", "0.78", GOOD_SPAN, NOTE)), cache);

            assertThat(cache.puts).hasSize(1);
            LlmCacheRepository.CachedCall row = cache.puts.getFirst();
            assertThat(row.promptVersion()).isEqualTo(PROMPT_VERSION);
            assertThat(row.model()).isEqualTo("TEST-model");
            assertThat(row.cacheKey())
                    .isEqualTo(PropositionChecker.cacheKey(PROMPT_VERSION, CITED_ECLI, CLAIM));
            assertThat(row.request()).contains(PROMPT_VERSION).contains(CLAIM);
            assertThat(row.response()).contains("OVERBROAD").contains("evidence_span");
        }

        @Test
        @DisplayName("quoting the holding rather than the passages is still evidence")
        void mayQuoteTheRatio() {
            StubLlm llm = new StubLlm(reply("OVERBROAD", "0.7", "jen u věcí movitých", NOTE));

            assertThat(check(llm, new StubCache()).evidenceSpan()).isEqualTo("jen u věcí movitých");
        }

        @Test
        @DisplayName("wrapped in a fenced code block, which models like to do")
        void survivesFencing() {
            StubLlm llm = new StubLlm(
                    "Here is the JSON:\n```json\n" + reply("UNRELATED", "0.55", GOOD_SPAN, NOTE) + "```\n");

            assertThat(check(llm, new StubCache()).verdict()).isEqualTo(PropositionVerdict.UNRELATED);
            assertThat(llm.prompts).hasSize(1);
        }
    }

    // ---------------------------------------------------------------- the gate

    @Nested
    @DisplayName("the evidence-span gate")
    class Gate {

        @Test
        @DisplayName("accepts a quote differing only in whitespace, without a retry")
        void acceptsWhitespaceOnlyDifferences() {
            String reflowed = "  závěry,   k nimž dospěl,\n se vztahují pouze k věcem movitým ";
            StubLlm llm = new StubLlm(reply("OVERBROAD", "0.8", reflowed.replace("\n", "\\n"), NOTE));

            PropositionVerdict verdict = check(llm, new StubCache());

            assertThat(verdict.verdict()).isEqualTo(PropositionVerdict.OVERBROAD);
            assertThat(llm.prompts).hasSize(1);
        }

        @Test
        @DisplayName("accepts a quote whose spaces are non-breaking")
        void acceptsNonBreakingSpaces() {
            String span = "se" + NBSP + "vztahují" + NBSP + "pouze" + NBSP + "k věcem movitým";
            StubLlm llm = new StubLlm(reply("OVERBROAD", "0.8", span, NOTE));

            assertThat(check(llm, new StubCache()).verdict()).isEqualTo(PropositionVerdict.OVERBROAD);
            assertThat(llm.prompts).hasSize(1);
        }

        @Test
        @DisplayName("rejects a quote differing by one real character, and retries exactly once")
        void rejectsARealCharacterDifference() {
            String fabricated = "se vztahují pouze k věcem nemovitým";
            StubLlm llm = new StubLlm(
                    reply("CONTRADICTS", "0.9", fabricated, NOTE),
                    reply("OVERBROAD", "0.8", GOOD_SPAN, NOTE));
            StubCache cache = new StubCache();

            PropositionVerdict verdict = check(llm, cache);

            assertThat(llm.prompts).hasSize(2);
            assertThat(verdict.verdict()).isEqualTo(PropositionVerdict.OVERBROAD);
            assertThat(cache.puts).hasSize(1);
        }

        @Test
        @DisplayName("rejects a quote with the diacritics stripped")
        void rejectsDiacriticsFolding() {
            StubLlm llm = new StubLlm(
                    reply("OVERBROAD", "0.9", "se vztahuji pouze k vecem movitym", NOTE),
                    reply("OVERBROAD", "0.8", GOOD_SPAN, NOTE));

            check(llm, new StubCache());

            assertThat(llm.prompts).hasSize(2);
        }

        @Test
        @DisplayName("states the violation back, appended to the original prompt")
        void statesTheViolationBack() {
            String fabricated = "soud výslovně překonal svůj dřívější závěr";
            StubLlm llm = new StubLlm(
                    reply("CONTRADICTS", "0.9", fabricated, NOTE),
                    reply("OVERBROAD", "0.8", GOOD_SPAN, NOTE));

            check(llm, new StubCache());

            String retry = llm.prompts.get(1);
            assertThat(retry).startsWith(llm.prompts.getFirst());
            assertThat(retry)
                    .contains("## Correction")
                    .contains("evidence_span")
                    .contains(fabricated);
        }

        @Test
        void rejectsAnEmptySpan() {
            StubLlm llm = new StubLlm(
                    reply("SUPPORTS", "0.99", "", NOTE),
                    reply("SUPPORTS", "0.9", GOOD_SPAN, NOTE));

            check(llm, new StubCache());

            assertThat(llm.prompts).hasSize(2);
            assertThat(llm.prompts.get(1)).contains("`evidence_span` was empty");
        }
    }

    // ---------------------------------------------------------------- shape failures

    @Nested
    @DisplayName("a malformed reply")
    class Malformed {

        @Test
        void withNoJsonAtAllIsRetried() {
            StubLlm llm = new StubLlm(
                    "I cannot answer that.", reply("UNRELATED", "0.6", GOOD_SPAN, NOTE));

            check(llm, new StubCache());

            assertThat(llm.prompts).hasSize(2);
            assertThat(llm.prompts.get(1)).contains("no JSON object");
        }

        @Test
        void withBrokenJsonIsRetried() {
            StubLlm llm = new StubLlm(
                    "{\"verdict\": \"SUPPORTS\", ", reply("SUPPORTS", "0.6", GOOD_SPAN, NOTE));

            check(llm, new StubCache());

            assertThat(llm.prompts).hasSize(2);
        }

        @Test
        @DisplayName("with a verdict outside the four is retried")
        void withAnInventedVerdictIsRetried() {
            StubLlm llm = new StubLlm(
                    reply("MOSTLY_SUPPORTS", "0.9", GOOD_SPAN, NOTE),
                    reply("SUPPORTS", "0.9", GOOD_SPAN, NOTE));

            check(llm, new StubCache());

            assertThat(llm.prompts).hasSize(2);
            assertThat(llm.prompts.get(1)).contains("MOSTLY_SUPPORTS");
        }

        @Test
        @DisplayName("claiming UNCLASSIFIED is retried: that state is ours to assign, not the model's")
        void cannotDeclareItselfUnclassified() {
            StubLlm llm = new StubLlm(
                    reply("UNCLASSIFIED", "0.0", GOOD_SPAN, NOTE),
                    reply("UNRELATED", "0.5", GOOD_SPAN, NOTE));

            assertThat(check(llm, new StubCache()).verdict()).isEqualTo(PropositionVerdict.UNRELATED);
            assertThat(llm.prompts).hasSize(2);
        }

        @Test
        void withAConfidenceOutOfRangeIsRetried() {
            StubLlm llm = new StubLlm(
                    reply("SUPPORTS", "1.5", GOOD_SPAN, NOTE),
                    reply("SUPPORTS", "0.9", GOOD_SPAN, NOTE));

            check(llm, new StubCache());

            assertThat(llm.prompts).hasSize(2);
            assertThat(llm.prompts.get(1)).contains("`confidence`");
        }
    }

    // ---------------------------------------------------------------- second failure

    @Nested
    @DisplayName("two failures running")
    class TwiceRejected {

        @Test
        @DisplayName("produce UNCLASSIFIED, in Czech, and store nothing")
        void produceUnclassified() {
            StubLlm llm = new StubLlm(
                    reply("CONTRADICTS", "0.9", "soud tento závěr překonal", NOTE),
                    reply("CONTRADICTS", "0.9", "soud tento závěr opět překonal", NOTE));
            StubCache cache = new StubCache();

            PropositionVerdict verdict = check(llm, cache);

            assertThat(verdict.isUnclassified()).isTrue();
            assertThat(verdict.verdict()).isEqualTo(PropositionVerdict.UNCLASSIFIED);
            assertThat(verdict.confidence()).isZero();
            assertThat(verdict.evidenceSpan()).isEmpty();
            assertThat(verdict.note()).contains("nezdařilo");
            assertThat(llm.prompts).hasSize(2);
            assertThat(cache.puts).isEmpty();
        }

        @Test
        @DisplayName("do not try a third time")
        void stopAfterTheRetry() {
            StubLlm llm = new StubLlm(
                    reply("SUPPORTS", "0.9", "vymyšlená citace", NOTE),
                    reply("SUPPORTS", "0.9", "další vymyšlená citace", NOTE));

            check(llm, new StubCache());

            assertThat(llm.prompts).hasSize(2);
            assertThat(llm.exhausted()).isTrue();
        }
    }

    // ---------------------------------------------------------------- caching

    @Nested
    @DisplayName("the cache")
    class Cache {

        @Test
        @DisplayName("replaces the second call entirely")
        void makesTheSecondCheckFree() {
            StubCache cache = new StubCache();
            PropositionVerdict first =
                    check(new StubLlm(reply("OVERBROAD", "0.78", GOOD_SPAN, NOTE)), cache);

            StubLlm silent = new StubLlm();
            PropositionVerdict replayed = check(silent, cache);

            assertThat(silent.prompts).isEmpty();
            assertThat(replayed).isEqualTo(first);
        }

        @Test
        @DisplayName("hits on a claim that differs only in whitespace")
        void normalisesTheClaimIntoTheKey() {
            StubCache cache = new StubCache();
            check(new StubLlm(reply("OVERBROAD", "0.78", GOOD_SPAN, NOTE)), cache);

            StubLlm silent = new StubLlm();
            PropositionVerdict replayed = new PropositionChecker(silent, cache)
                    .check(
                            CITED_ECLI,
                            CITED_COURT,
                            CITED_DATE,
                            RATIO,
                            CONTEXT,
                            "Rozhodnutí   platí pro všechny\n věci bez rozdílu.");

            assertThat(silent.prompts).isEmpty();
            assertThat(replayed.verdict()).isEqualTo(PropositionVerdict.OVERBROAD);
        }

        @Test
        @DisplayName("is keyed on (cited_ecli, claim_hash, prompt_version), per the frontmatter")
        void usesTheDocumentedKey() {
            String key = PropositionChecker.cacheKey(PROMPT_VERSION, CITED_ECLI, CLAIM);

            assertThat(key).startsWith(PROMPT_VERSION + "|" + CITED_ECLI + "|");
            assertThat(key.substring(key.lastIndexOf('|') + 1)).hasSize(64).containsPattern("^[0-9a-f]+$");
            assertThat(PropositionChecker.cacheKey(PROMPT_VERSION, CITED_ECLI, "jiné tvrzení"))
                    .isNotEqualTo(key);
        }

        @Test
        @DisplayName("a stored reply that no longer quotes the passages is discarded, not served")
        void rechecksTheStoredSpan() {
            StubCache cache = new StubCache();
            cache.put(new LlmCacheRepository.CachedCall(
                    PropositionChecker.cacheKey(PROMPT_VERSION, CITED_ECLI, CLAIM),
                    PROMPT_VERSION,
                    "TEST-model",
                    "{}",
                    reply("CONTRADICTS", "0.9", "text, který v rozhodnutí není", NOTE)));

            StubLlm llm = new StubLlm(reply("OVERBROAD", "0.78", GOOD_SPAN, NOTE));
            PropositionVerdict verdict = check(llm, cache);

            assertThat(llm.prompts).hasSize(1);
            assertThat(verdict.verdict()).isEqualTo(PropositionVerdict.OVERBROAD);
        }
    }

    // ---------------------------------------------------------------- no model

    @Nested
    @DisplayName("with no model available")
    class Unavailable {

        @Test
        @DisplayName("the failure propagates: unchecked is not the same as unclassified")
        void propagates() {
            LlmClient unconfigured = new LlmClient.Unconfigured("ANTHROPIC_API_KEY is not set");
            StubCache cache = new StubCache();

            assertThatThrownBy(() -> new PropositionChecker(unconfigured, cache)
                    .check(CITED_ECLI, CITED_COURT, CITED_DATE, RATIO, CONTEXT, CLAIM))
                    .isInstanceOf(LlmClient.Unavailable.class)
                    .hasMessageContaining("ANTHROPIC_API_KEY");
            assertThat(cache.puts).isEmpty();
        }
    }

    // ---------------------------------------------------------------- helpers

    private static PropositionVerdict check(LlmClient llm, LlmCacheRepository cache) {
        return new PropositionChecker(llm, cache)
                .check(CITED_ECLI, CITED_COURT, CITED_DATE, RATIO, CONTEXT, CLAIM);
    }

    private static String reply(String verdict, String confidence, String span, String note) {
        return """
                {
                  "verdict": "%s",
                  "confidence": %s,
                  "evidence_span": "%s",
                  "note": "%s"
                }
                """.formatted(verdict, confidence, span, note);
    }

    /** Hands out canned replies in order and fails the test on an unexpected extra call. */
    private static final class StubLlm implements LlmClient {

        private final Deque<String> replies = new ArrayDeque<>();
        private final List<String> prompts = new ArrayList<>();

        StubLlm(String... replies) {
            this.replies.addAll(List.of(replies));
        }

        @Override
        public String complete(String prompt) {
            prompts.add(prompt);
            String reply = replies.poll();
            if (reply == null) {
                throw new AssertionError("unexpected model call " + prompts.size() + ":\n" + prompt);
            }
            return reply;
        }

        @Override
        public String model() {
            return "TEST-model";
        }

        boolean exhausted() {
            return replies.isEmpty();
        }
    }

    private static final class StubCache implements LlmCacheRepository {

        private final Map<String, CachedCall> rows = new LinkedHashMap<>();
        private final List<CachedCall> puts = new ArrayList<>();

        @Override
        public Optional<CachedCall> find(String cacheKey) {
            return Optional.ofNullable(rows.get(cacheKey));
        }

        @Override
        public void put(CachedCall call) {
            puts.add(call);
            rows.put(call.cacheKey(), call);
        }
    }
}

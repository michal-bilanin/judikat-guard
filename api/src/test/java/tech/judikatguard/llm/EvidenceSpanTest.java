package tech.judikatguard.llm;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

/**
 * The hallucination gate. D3 and CLAUDE.md rule 3.
 *
 * <p>Half of these tests exist to pin down what the gate must <em>not</em> accept. If one of
 * them starts failing because a model reply "was nearly right", the reply is the thing to
 * fix.
 *
 * <p>The Czech sentences are prose, not identifiers, so nothing here can resolve against
 * crawled data.
 */
class EvidenceSpanTest {

    /** U+00A0. Arrives in quantity from the courts' HTML, which is why it is folded. */
    private static final String NBSP = "\u00A0";

    /** U+202F, narrow no-break space; U+2007, figure space. Both are typeset Czech. */
    private static final String NNBSP = "\u202F";

    private static final String FIGURE_SPACE = "\u2007";

    /** The worked example from PLAN.md section 12, as a paragraph a court would write. */
    private static final String CONTEXT = """
            Nejvyšší správní soud předesílá, že závěry, k nimž dospěl v citovaném rozsudku,
            se vztahují pouze k věcem movitým.

            Na nemovité věci proto tyto závěry bez dalšího vztáhnout nelze.""";

    @Nested
    @DisplayName("normalizeWhitespace")
    class Normalize {

        @Test
        void collapsesEveryKindOfWhitespaceToOneSpace() {
            assertThat(EvidenceSpan.normalizeWhitespace("a  b\tc\nd\r\ne"))
                    .isEqualTo("a b c d e");
        }

        @Test
        void collapsesNonBreakingAndNarrowSpaces() {
            assertThat(EvidenceSpan.normalizeWhitespace("§" + NBSP + "2000" + NNBSP + "odst." + FIGURE_SPACE + "1"))
                    .isEqualTo("§ 2000 odst. 1");
        }

        @Test
        void trimsIncludingNonBreakingEnds() {
            assertThat(EvidenceSpan.normalizeWhitespace(NBSP + " \n věcem movitým \t" + NBSP))
                    .isEqualTo("věcem movitým");
        }

        @Test
        void isIdempotent() {
            String once = EvidenceSpan.normalizeWhitespace(CONTEXT);
            assertThat(EvidenceSpan.normalizeWhitespace(once)).isEqualTo(once);
        }

        @Test
        @DisplayName("leaves letters, diacritics and punctuation alone")
        void changesNothingButWhitespace() {
            assertThat(EvidenceSpan.normalizeWhitespace("Věc: § 2000, odst. 1 – movité věci!"))
                    .isEqualTo("Věc: § 2000, odst. 1 – movité věci!");
        }
    }

    @Nested
    @DisplayName("isPresentIn accepts")
    class Accepts {

        @Test
        void anExactQuote() {
            assertThat(EvidenceSpan.isPresentIn("se vztahují pouze k věcem movitým", CONTEXT)).isTrue();
        }

        @Test
        @DisplayName("a quote whose line break the model replaced with a space")
        void aQuoteDifferingOnlyInWhitespace() {
            assertThat(EvidenceSpan.isPresentIn(
                            "závěry, k nimž dospěl v citovaném rozsudku, se vztahují pouze k věcem movitým",
                            CONTEXT))
                    .isTrue();
        }

        @Test
        void aQuotePaddedAndReflowed() {
            assertThat(EvidenceSpan.isPresentIn("\n   se vztahují   pouze\tk věcem movitým  \n", CONTEXT))
                    .isTrue();
        }

        @Test
        @DisplayName("a non-breaking space in the context against a plain space in the quote")
        void aQuoteWhereTheContextHasNbsp() {
            String context = "Soud vyšel z §" + NBSP + "2000 odst." + NBSP + "1 občanského zákoníku.";
            assertThat(EvidenceSpan.isPresentIn("z § 2000 odst. 1 občanského zákoníku", context)).isTrue();
        }

        @Test
        @DisplayName("a non-breaking space in the quote against a plain space in the context")
        void aQuoteWhichItselfHasNbsp() {
            assertThat(EvidenceSpan.isPresentIn("k" + NBSP + "věcem" + NBSP + "movitým", CONTEXT)).isTrue();
        }

        @Test
        @DisplayName("a quote from the second context, e.g. the ratio rather than the paragraphs")
        void aQuoteFromAnyOneOfTheContexts() {
            String ratio = "Závěr o odpovědnosti se uplatní jen u věcí movitých.";
            assertThat(EvidenceSpan.isPresentIn("jen u věcí movitých", ratio, CONTEXT)).isTrue();
            assertThat(EvidenceSpan.isPresentIn("pouze k věcem movitým", ratio, CONTEXT)).isTrue();
        }
    }

    @Nested
    @DisplayName("isPresentIn rejects")
    class Rejects {

        @Test
        @DisplayName("a quote differing by one real character")
        void aQuoteWithASubstitutedLetter() {
            assertThat(EvidenceSpan.isPresentIn("se vztahují pouze k věcem movitém", CONTEXT)).isFalse();
        }

        @Test
        @DisplayName("a quote with the diacritics stripped: no folding, ever")
        void aQuoteWithoutDiacritics() {
            assertThat(EvidenceSpan.isPresentIn("se vztahuji pouze k vecem movitym", CONTEXT)).isFalse();
        }

        @Test
        void aQuoteWithDifferentCase() {
            assertThat(EvidenceSpan.isPresentIn("Se Vztahují Pouze K Věcem Movitým", CONTEXT)).isFalse();
        }

        @Test
        @DisplayName("a paraphrase, however faithful")
        void aParaphrase() {
            assertThat(EvidenceSpan.isPresentIn("závěry platí jen pro movité věci", CONTEXT)).isFalse();
        }

        @Test
        @DisplayName("a quote with punctuation the court did not write")
        void aQuoteWithAddedPunctuation() {
            assertThat(EvidenceSpan.isPresentIn("se vztahují pouze k věcem movitým!", CONTEXT)).isFalse();
        }

        @Test
        @DisplayName("the empty quote, which is a substring of everything")
        void anEmptyOrBlankQuote() {
            assertThat(EvidenceSpan.isPresentIn("", CONTEXT)).isFalse();
            assertThat(EvidenceSpan.isPresentIn("   \n" + NBSP, CONTEXT)).isFalse();
        }

        @Test
        void anyQuoteWhenNoContextWasSupplied() {
            assertThat(EvidenceSpan.isPresentIn("k věcem movitým")).isFalse();
            assertThat(EvidenceSpan.isPresentIn("k věcem movitým", "")).isFalse();
        }
    }
}

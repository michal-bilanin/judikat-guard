package tech.judikatguard.llm;

import java.util.regex.Pattern;

/**
 * The hallucination gate. D3 and CLAUDE.md rule 3.
 *
 * <p>Every model reply must quote the context it was given. Normalise whitespace, then
 * require the quote to be a literal substring of that context. A reply that fails is retried
 * once with the violation stated back to the model; a second failure is recorded as
 * {@code UNCLASSIFIED} and surfaced as amber "needs review". It is never stored as a label.
 *
 * <p><b>Whitespace and nothing else.</b> No case folding, no diacritics folding, no
 * punctuation stripping, no edit distance, no token overlap. Those all sound like small
 * conveniences and each one reopens the hole this check exists to close: a model that
 * paraphrases <em>věcí movitých</em> as <em>veci movite</em> has stopped quoting the
 * decision, and a tool whose evidence panel shows a quote the source document does not
 * contain is worse than no tool. Whitespace is the one difference that is provably an
 * artefact of how the context was assembled (HTML, non-breaking spaces from the courts'
 * pages, paragraph joins) rather than a difference in what was said.
 *
 * <p>Do not relax either method to make a test pass.
 */
public final class EvidenceSpan {

    /**
     * Every kind of whitespace that shows up in text scraped from the courts' pages and the
     * e-Sbírka dumps: ASCII whitespace, NEL, and the Unicode separator categories, which is
     * where the non-breaking space U+00A0, the narrow no-break space U+202F and the figure
     * space U+2007 live. Zero-width characters are deliberately absent: they are format
     * characters, not whitespace, and folding them would be a fuzzy match.
     */
    private static final Pattern WHITESPACE = Pattern.compile("[\\s\\u0085\\p{Zs}\\p{Zl}\\p{Zp}]+");

    private EvidenceSpan() {}

    /**
     * Collapses every run of whitespace to a single ASCII space and trims the ends.
     *
     * <p>Idempotent, and the only transformation applied before the substring test. Applied
     * to both sides so that a quote copied out of a rendered paragraph still matches the
     * paragraph as stored.
     */
    public static String normalizeWhitespace(String s) {
        return WHITESPACE.matcher(s).replaceAll(" ").trim();
    }

    /**
     * Whether {@code span} is a verbatim quote of at least one of {@code contexts}, compared
     * after {@link #normalizeWhitespace}.
     *
     * <p>A blank span returns {@code false}. That case matters: the empty string is a
     * substring of everything, so accepting it would let a model satisfy the gate by
     * quoting nothing, which is precisely the failure mode being guarded against.
     *
     * @param contexts every text the model was actually shown, e.g. the cited decision's
     *     {@code ratio_summary} and the paragraph window. Pass an empty string for a
     *     decision with no {@code právní věta} rather than omitting the argument.
     */
    public static boolean isPresentIn(String span, String... contexts) {
        String needle = normalizeWhitespace(span);
        if (needle.isEmpty()) {
            return false;
        }
        for (String context : contexts) {
            if (normalizeWhitespace(context).contains(needle)) {
                return true;
            }
        }
        return false;
    }
}

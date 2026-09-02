package tech.judikatguard.extract;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.regex.Pattern;
import java.util.stream.Gatherers;
import java.util.stream.IntStream;
import java.util.stream.Stream;

/**
 * Paragraph handling for an uploaded document: splitting, whitespace folding, and the
 * context window the classifier is given.
 *
 * <p>Paragraph indices are 1-based throughout, matching {@code decision_paragraph.idx} and
 * the Python side's {@code extract_paragraphs}, which enumerates from 1.
 */
public final class Paragraphs {

    /** Paragraphs either side of the citation that go into the classifier context. */
    public static final int CONTEXT_RADIUS = 2;

    /** {@code 2 * CONTEXT_RADIUS + 1}: the sliding window that produces one context. */
    private static final int WINDOW = 2 * CONTEXT_RADIUS + 1;

    /**
     * Every character Python's Unicode-aware {@code \s} matches. Java's {@code \s} is
     * ASCII-only, so folding this class to a plain space before any pattern runs is what
     * makes the ASCII {@code \s} inside {@code extract/patterns.toml} behave identically in
     * both runtimes. NBSP in particular is everywhere in scraped court text.
     */
    private static final Pattern WHITESPACE =
            Pattern.compile("[ \\t\\n\\x0B\\f\\r\\x1C-\\x1F\\u0085\\u00A0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000]+");

    /** A blank line: the paragraph separator in a pasted document. */
    private static final Pattern BLANK_LINE = Pattern.compile("\\R[ \\t\\u00A0]*\\R");

    /** Any single line terminator, the fallback separator. */
    private static final Pattern LINE = Pattern.compile("\\R");

    private Paragraphs() {}

    /**
     * Collapse every run of whitespace to one space and trim. Mirrors
     * {@code jg.crawl.base.normalise_ws}, which is how paragraphs are stored, so a
     * {@link Reference} offset means the same thing on both sides.
     */
    public static String normalise(String text) {
        return WHITESPACE.matcher(text).replaceAll(" ").strip();
    }

    /**
     * Split a document into normalised paragraphs, dropping blank ones.
     *
     * <p>A blank line is the separator. A document that has no blank line but does have line
     * terminators is treated as one paragraph per line, which is how court text arrives once
     * the {@code <br/>} runs have been flattened. Consequence worth knowing: a hard-wrapped
     * document with no blank lines yields one paragraph per wrapped line. Nothing downstream
     * breaks — the context window widens to compensate — but the paragraph indices will not
     * match a print layout.
     *
     * <p>Blank paragraphs are dropped before numbering, exactly as
     * {@code jg.crawl.base.text_paragraphs} does, so index 1 is the first paragraph that has
     * any text in it.
     */
    public static List<String> split(String documentText) {
        Objects.requireNonNull(documentText, "documentText");
        List<String> paragraphs = normaliseAll(BLANK_LINE.split(documentText));
        if (paragraphs.size() <= 1 && LINE.matcher(documentText).find()) {
            List<String> perLine = normaliseAll(LINE.split(documentText));
            if (perLine.size() > paragraphs.size()) {
                return perLine;
            }
        }
        return paragraphs;
    }

    private static List<String> normaliseAll(String[] chunks) {
        List<String> out = new ArrayList<>(chunks.length);
        for (String chunk : chunks) {
            String cleaned = normalise(chunk);
            if (!cleaned.isEmpty()) {
                out.add(cleaned);
            }
        }
        return List.copyOf(out);
    }

    /**
     * The classifier's context: a citation's paragraph plus {@value #CONTEXT_RADIUS} either
     * side (PLAN.md section 8, prompt contract).
     *
     * @param centreIdx  the paragraph the citation sits in, 1-based
     * @param fromIdx    first paragraph actually included; equals {@code centreIdx} at the
     *                   start of a document, where there is nothing before it
     * @param toIdx      last paragraph actually included
     * @param paragraphs the bodies, {@code fromIdx}..{@code toIdx} in order
     */
    public record ContextWindow(int centreIdx, int fromIdx, int toIdx, List<String> paragraphs) {

        public ContextWindow {
            paragraphs = List.copyOf(paragraphs);
        }

        /**
         * The window as one string, blank line between paragraphs. This is the text the
         * model sees, and therefore the text an {@code evidence_span} must be a substring of
         * after whitespace normalisation (CLAUDE.md rule 3).
         */
        public String text() {
            return String.join("\n\n", paragraphs);
        }
    }

    /**
     * One context window per paragraph, in order: element {@code n} is centred on paragraph
     * {@code n + 1}.
     *
     * <p>Built with {@link Gatherers#windowSliding(int)} over the paragraph list padded with
     * {@value #CONTEXT_RADIUS} placeholders at each end, so the edges need no special case:
     * a citation in paragraph 1 simply has two of its five slots filled by padding, which is
     * then filtered out. Padding is what keeps this free of index arithmetic.
     */
    public static List<ContextWindow> contextWindows(List<String> paragraphs) {
        Objects.requireNonNull(paragraphs, "paragraphs");
        if (paragraphs.isEmpty()) {
            return List.of();
        }
        return padded(paragraphs)
                .gather(Gatherers.windowSliding(WINDOW))
                .map(Paragraphs::toWindow)
                .toList();
    }

    /**
     * The context window centred on one paragraph, or empty when {@code paragraphIdx} is
     * outside the document — which happens for a stored {@code citation.paragraph_idx} that
     * the extractor could not place.
     *
     * <p>Builds every window and keeps one. Call {@link #contextWindows(List)} once instead
     * when you need the context for many citations in the same document.
     */
    public static Optional<ContextWindow> contextWindow(List<String> paragraphs, int paragraphIdx) {
        Objects.requireNonNull(paragraphs, "paragraphs");
        if (paragraphIdx < 1 || paragraphIdx > paragraphs.size()) {
            return Optional.empty();
        }
        return Optional.of(contextWindows(paragraphs).get(paragraphIdx - 1));
    }

    /**
     * Chunk a list into fixed-size batches with {@link Gatherers#windowFixed(int)}. Used to
     * turn a document's citations into classification requests; the last batch is short
     * rather than padded.
     */
    public static <T> List<List<T>> batches(List<T> items, int size) {
        Objects.requireNonNull(items, "items");
        if (size < 1) {
            throw new IllegalArgumentException("batch size must be positive: " + size);
        }
        return items.stream().gather(Gatherers.windowFixed(size)).toList();
    }

    /** One paragraph, or a padding slot at the edge of the document. Padding carries idx 0. */
    private record Slot(int idx, String body) {
        static final Slot PADDING = new Slot(0, "");

        boolean real() {
            return idx > 0;
        }
    }

    private static Stream<Slot> padded(List<String> paragraphs) {
        List<Slot> pad = Collections.nCopies(CONTEXT_RADIUS, Slot.PADDING);
        Stream<Slot> body =
                IntStream.rangeClosed(1, paragraphs.size())
                        .mapToObj(idx -> new Slot(idx, paragraphs.get(idx - 1)));
        return Stream.concat(Stream.concat(pad.stream(), body), pad.stream());
    }

    private static ContextWindow toWindow(List<Slot> window) {
        List<Slot> real = window.stream().filter(Slot::real).toList();
        return new ContextWindow(
                window.get(CONTEXT_RADIUS).idx(),
                real.getFirst().idx(),
                real.getLast().idx(),
                real.stream().map(Slot::body).toList());
    }
}

package tech.judikatguard.extract;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Objects;
import tech.judikatguard.extract.PatternSet.CompiledPattern;

/**
 * The rules pass over a document uploaded at query time. Java twin of
 * {@code jg.extract.patterns.find_references} (PLAN.md section 7).
 *
 * <p>Both implementations load the same {@code extract/patterns.toml} and must produce
 * byte-identical output on {@code eval/extraction-golden.jsonl}; {@code ExtractionParityTest}
 * fails the build otherwise. That means every behavioural decision here is a mirror of a
 * decision on the Python side rather than a free choice — in particular the overlap policy
 * in {@link #dropContained(List)} and the final sort order.
 *
 * <p>Resolution against {@code decision_alias} and provision anaphora are separate steps;
 * this class only finds spans.
 *
 * <p>Stateless and thread-safe once constructed. There is no {@code @Component} annotation
 * on purpose: the pattern set is a loaded file, not a bean, so wire it explicitly with
 * {@code new CitationExtractor(PatternSet.fromClasspathOrRepo())} or use
 * {@link #withSharedPatterns()}.
 */
public final class CitationExtractor {

    /** Final order: by start, then by end. Declaration order breaks remaining ties. */
    private static final Comparator<Reference> DOCUMENT_ORDER =
            Comparator.comparingInt(Reference::start).thenComparingInt(Reference::end);

    private final PatternSet patterns;

    public CitationExtractor(PatternSet patterns) {
        this.patterns = Objects.requireNonNull(patterns, "patterns");
    }

    /** Extractor over the shared {@code extract/patterns.toml}. See {@link PatternSet#fromClasspathOrRepo()}. */
    public static CitationExtractor withSharedPatterns() {
        return new CitationExtractor(PatternSet.fromClasspathOrRepo());
    }

    /** The pattern set this extractor runs, for callers that also need the markers. */
    public PatternSet patterns() {
        return patterns;
    }

    /**
     * Every citation-like span in a whole document, in document order.
     *
     * <p>The document is split by {@link Paragraphs#split(String)} and each paragraph is
     * numbered from 1. {@link Reference#start()} and {@link Reference#end()} are offsets into
     * the <em>normalised paragraph body</em>, not into the text passed in here: that is what
     * makes an offset stored against {@code decision_paragraph} mean the same thing as an
     * offset computed at query time.
     */
    public List<Reference> extract(String documentText) {
        List<String> paragraphs = Paragraphs.split(documentText);
        List<Reference> references = new ArrayList<>();
        for (int paragraphIdx = 1; paragraphIdx <= paragraphs.size(); paragraphIdx++) {
            references.addAll(extractParagraph(paragraphs.get(paragraphIdx - 1), paragraphIdx));
        }
        return List.copyOf(references);
    }

    /**
     * Every citation-like span in one paragraph, in order of appearance.
     *
     * <p>{@code body} is expected to be normalised already (it is, when it comes from
     * {@link Paragraphs#split(String)} or from {@code decision_paragraph.body}); offsets are
     * relative to it.
     */
    public List<Reference> extractParagraph(String body, int paragraphIdx) {
        Objects.requireNonNull(body, "body");
        List<Reference> candidates = new ArrayList<>();
        // Pattern-declaration order, because dropContained uses list position as its
        // tie-break for identical spans.
        for (CompiledPattern pattern : patterns.patterns()) {
            candidates.addAll(pattern.find(body, paragraphIdx));
        }
        List<Reference> kept = dropContained(candidates);
        kept.sort(DOCUMENT_ORDER);
        return List.copyOf(kept);
    }

    /**
     * Overlap policy for spans produced by different patterns. Mirrors
     * {@code jg.extract.patterns.drop_contained}, and must keep mirroring it.
     *
     * <ul>
     *   <li><strong>Keep the longest match.</strong> A span fully contained in a strictly
     *       longer span is dropped, whatever pattern produced it. {@code case_no_us} and
     *       {@code case_no_gen} are both anchored on {@code sp. zn.}, and the
     *       {@code provision} act-number tail can swallow a shorter citation.
     *   <li><strong>Partial overlap is kept.</strong> Two spans that merely cross are both
     *       real references; dropping either loses a citation.
     *   <li><strong>Identical spans: the pattern declared first in
     *       {@code extract/patterns.toml} wins.</strong> Declaration order is the tie-break
     *       so the outcome is reviewable in the TOML rather than dependent on iteration
     *       order.
     * </ul>
     *
     * @param candidates in pattern-declaration order, which is what
     *     {@link #extractParagraph(String, int)} builds
     * @return a mutable list, in the same relative order as {@code candidates}
     */
    static List<Reference> dropContained(List<Reference> candidates) {
        List<Reference> kept = new ArrayList<>(candidates.size());
        for (int position = 0; position < candidates.size(); position++) {
            Reference candidate = candidates.get(position);
            boolean suppressed = false;
            for (int otherPosition = 0; otherPosition < candidates.size(); otherPosition++) {
                if (otherPosition == position) {
                    continue;
                }
                if (covers(candidates.get(otherPosition), candidate, otherPosition < position)) {
                    suppressed = true;
                    break;
                }
            }
            if (!suppressed) {
                kept.add(candidate);
            }
        }
        return kept;
    }

    /** True when {@code outer} suppresses {@code inner} under the overlap policy. */
    private static boolean covers(Reference outer, Reference inner, boolean outerDeclaredFirst) {
        if (!outer.contains(inner)) {
            return false;
        }
        if (outer.length() > inner.length()) {
            return true;
        }
        return outerDeclaredFirst;
    }
}

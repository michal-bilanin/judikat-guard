/**
 * Query-time citation extraction: the Java twin of {@code pipeline/jg/extract/patterns.py}.
 *
 * <p>Extraction runs in two places (PLAN.md section 7). Python runs it in batch over the
 * crawled corpus; this package runs it over a document pasted into the web page. Neither
 * side owns the regexes: both load {@code extract/patterns.toml} at the repo root, and
 * {@code eval/extraction-golden.jsonl} pins the output both must reproduce. The parity test
 * fails the build on divergence, so nothing in this package may hard-code a pattern.
 *
 * <p>Two traps this package exists to avoid:
 *
 * <ul>
 *   <li>{@code java.util.regex} treats {@code \w}, {@code \d} and {@code \s} as ASCII-only
 *       while Python 3 treats them as Unicode-aware. {@code extract/patterns.toml} is
 *       written to dodge that difference, and {@link tech.judikatguard.extract.Paragraphs}
 *       folds every Unicode space to {@code U+0020} before a pattern ever sees the text.
 *       Do <em>not</em> add {@link java.util.regex.Pattern#UNICODE_CHARACTER_CLASS}: it
 *       would change what the shared patterns match and break parity.
 *   <li>Offsets. {@code Reference.start} and {@code Reference.end} index into the
 *       paragraph body, never into the whole document, exactly as on the Python side.
 * </ul>
 */
@NullMarked
package tech.judikatguard.extract;

import org.jspecify.annotations.NullMarked;

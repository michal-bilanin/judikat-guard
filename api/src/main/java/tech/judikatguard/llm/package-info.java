/**
 * The model-call layer: prompt loading, the HTTP client, the hallucination gate and the
 * proposition check. PLAN.md sections 8, 10 and 12.
 *
 * <p>Three invariants hold everywhere in this package.
 *
 * <ol>
 *   <li><b>No stored label without a verbatim quote.</b> {@link tech.judikatguard.llm.EvidenceSpan}
 *       is the only gate: normalise whitespace, then require a literal substring of the
 *       context that was actually supplied to the model. One retry with the violation stated
 *       back, then {@code UNCLASSIFIED}. Nothing here does fuzzy, stemmed or
 *       diacritics-folded matching, and nothing may be relaxed to make a test pass
 *       (CLAUDE.md rule 3, D3).</li>
 *   <li><b>Every call is cached and every cached row records {@code model} and
 *       {@code prompt_version}.</b> The cache key is the one documented in the frontmatter of
 *       the prompt that produced the call, not one invented here
 *       ({@link tech.judikatguard.llm.LlmCacheRepository}).</li>
 *   <li><b>The prompt version is read from the file, never hardcoded.</b>
 *       {@link tech.judikatguard.llm.PromptTemplate} fails loudly rather than render a
 *       half-substituted prompt, because a silently truncated prompt produces a
 *       plausible-looking label that nothing downstream can detect.</li>
 * </ol>
 *
 * <p>This package produces labels only. It never assigns a traffic light; that is
 * {@code tech.judikatguard.status} and it stays free of I/O (D2).
 */
@NullMarked
package tech.judikatguard.llm;

import org.jspecify.annotations.NullMarked;

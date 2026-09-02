/**
 * Orchestration: extract, resolve, read the graph, assemble a report.
 *
 * <p>This package is the seam between the HTTP layer and the two layers that carry the
 * project's rules. It owns the read path of PLAN.md sections 11 and 12 and it decides
 * nothing about verdicts: {@link tech.judikatguard.status.StatusEngine} does that, from
 * evidence assembled here.
 *
 * <p>Two invariants.
 *
 * <ol>
 *   <li><b>An uploaded document writes nothing.</b> Extraction runs at query time and the
 *       results are resolved against {@code decision_alias} and read against the existing
 *       graph. Nothing is inserted into {@code citation}: that table's {@code citing_ecli}
 *       is {@code not null} because it describes the crawled corpus, and an uploaded
 *       document has no place in it (PLAN.md section 11).</li>
 *   <li><b>A reference that does not resolve is reported, never dropped.</b> It goes into
 *       the {@code unresolved} array of {@link tech.judikatguard.document.DocumentReport}
 *       with a reason. Being explicit about coverage gaps is the product.</li>
 * </ol>
 *
 * <p>Request-scoped state — the date asked about, the corpus coverage, the active prompt
 * version — travels in {@link tech.judikatguard.document.EvaluationContext}, a
 * {@code ScopedValue} bound once per request, rather than as a parameter threaded through
 * every repository signature. It is never read inside the {@code status} package.
 */
@NullMarked
package tech.judikatguard.document;

import org.jspecify.annotations.NullMarked;

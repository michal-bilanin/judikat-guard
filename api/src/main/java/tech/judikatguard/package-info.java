/**
 * Judikát Guard: an automated validity checker for Czech legal sources.
 *
 * <p>Four packages, in dependency order:
 *
 * <ol>
 *   <li>{@link tech.judikatguard.status} — the verdict layer. Pure, no I/O (D2).</li>
 *   <li>{@link tech.judikatguard.extract} — the query-time rules pass over an uploaded
 *       document, twin of the Python extractor.</li>
 *   <li>{@link tech.judikatguard.decision} — hand-written SQL over {@code JdbcClient}:
 *       the evidence the verdict layer consumes.</li>
 *   <li>{@link tech.judikatguard.document} — orchestration: extract, resolve, read the
 *       graph, assemble a report.</li>
 *   <li>{@link tech.judikatguard.web} — controllers and error handling. Nothing below this
 *       package knows about HTTP.</li>
 * </ol>
 */
@NullMarked
package tech.judikatguard;

import org.jspecify.annotations.NullMarked;

/**
 * The evidence layer: hand-written SQL that turns the citation graph into the records
 * {@code tech.judikatguard.status} evaluates.
 *
 * <p>Three rules hold across this package.
 *
 * <ol>
 *   <li><b>{@code JdbcClient} and explicit column lists.</b> No JPA, no ORM, no
 *       {@code select *} (PLAN.md section 4). The recursive and lateral joins here are the
 *       point of the project, not an implementation detail to hide.</li>
 *   <li><b>Legal authority comes from a table.</b> {@code citingMayDepart} is read from
 *       {@code departure_authority}, never decided in Java. A missing row means false, so
 *       an unknown court/panel combination degrades to "could not bind" rather than to a
 *       red light (D5).</li>
 *   <li><b>One hop.</b> {@link tech.judikatguard.decision.EvidenceRepository#whollyReliedOn}
 *       returns the immediate {@code FOLLOWED} sources and nothing deeper. Deeper
 *       propagation is unexplainable and noisy (PLAN.md section 9).</li>
 * </ol>
 *
 * <p>Nothing here assigns a light, and nothing here writes to {@code citation} or
 * {@code treatment}: those tables belong to the crawled corpus and are filled by the
 * Python pipeline.
 */
@NullMarked
package tech.judikatguard.decision;

import org.jspecify.annotations.NullMarked;

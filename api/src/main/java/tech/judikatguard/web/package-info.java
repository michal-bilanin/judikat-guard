/**
 * The HTTP layer: controllers, request-scoped context binding, error handling.
 *
 * <p>Records as DTOs, one endpoint per operation in PLAN.md section 11, and no business
 * rule anywhere. Two conventions matter.
 *
 * <ol>
 *   <li><b>Every user-facing string is Czech</b>, in the vocabulary of PLAN.md section 16.
 *       Czech law has no doctrine of binding precedent, so a translated common-law term
 *       ("overruled", "distinguished") is not a translation error but a substantive one, and
 *       a legal audience will read it as incompetence inside the first minute. The wording
 *       lives in {@link tech.judikatguard.web.Wording} and in {@code web/src/czech.ts}.</li>
 *   <li><b>Nothing is asserted to be valid.</b> Every answer carries the date it was
 *       computed for and the size and coverage of the corpus it was computed against
 *       (PLAN.md section 2).</li>
 * </ol>
 */
@NullMarked
package tech.judikatguard.web;

import org.jspecify.annotations.NullMarked;

/**
 * The verdict layer: pure, deterministic mapping from evidence rows to a traffic light.
 *
 * <p>Nothing in this package reads a clock, a repository or a configuration file. Every
 * input arrives through {@link tech.judikatguard.status.Evaluation}. See PLAN.md section 9.
 */
@NullMarked
package tech.judikatguard.status;

import org.jspecify.annotations.NullMarked;

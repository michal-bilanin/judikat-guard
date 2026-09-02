package tech.judikatguard.status;

/**
 * The traffic light shown next to a source.
 *
 * <p>GREEN is never rendered as "valid": the UI wording is "no adverse treatment found in
 * N decisions through date C". See PLAN.md section 2.
 */
public enum Light {
    GREEN,
    AMBER,
    RED
}

package tech.judikatguard.extract;

import java.util.Locale;

/**
 * What a regex in {@code extract/patterns.toml} matched.
 *
 * <p>Mirrors {@code jg.models.ReferenceKind}. The wire form is the lowercase enum name and
 * it is the {@code kind} field of a {@code [patterns.*]} table, of a
 * {@code eval/extraction-golden.jsonl} row, and of {@code decision_alias.alias_kind} for
 * the three kinds that are also aliases. Renaming a constant is a cross-runtime change.
 */
public enum ReferenceKind {
    /** {@code č. j.}, a reference number: {@code č. j. 6 Ads 45/2014-32}. */
    REF_NO,
    /** {@code sp. zn.}, a case number, in either the ÚS or the NSS/NS spelling. */
    CASE_NO,
    /** A full European Case Law Identifier. The primary key of {@code decision}. */
    ECLI,
    /** An R-číslo: publication in the official reporter, a signal of higher authority. */
    JOURNAL_NO,
    /** A statutory provision, with or without the act number written out. */
    PROVISION;

    /** The lowercase form used in the TOML, the golden fixture and the database. */
    public String wireName() {
        return name().toLowerCase(Locale.ROOT);
    }

    /**
     * Parse the wire form.
     *
     * @throws IllegalArgumentException if {@code wire} names no kind; an unknown {@code kind}
     *     in {@code extract/patterns.toml} is a contract break, not something to skip over
     */
    public static ReferenceKind fromWire(String wire) {
        for (ReferenceKind kind : values()) {
            if (kind.wireName().equals(wire)) {
                return kind;
            }
        }
        throw new IllegalArgumentException("unknown reference kind '" + wire + "'");
    }
}

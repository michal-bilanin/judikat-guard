package tech.judikatguard.document;

import java.util.Objects;

/**
 * A reference the document made that this system could not place.
 *
 * <p>Not an error, and never omitted: the {@code unresolved} array is a first-class field of
 * {@link DocumentReport}, because being explicit about coverage gaps is the product
 * (PLAN.md section 11). A tool that hides the citations it failed on cannot be trusted on
 * the ones it reports.
 *
 * @param reason the machine-readable reason, one of the values
 *     {@link ReferenceResolver#NOT_IN_CORPUS} / {@link ReferenceResolver#NOT_RESOLVED};
 *     {@code web/src/czech.ts} maps each to its Czech phrasing
 */
public record UnresolvedReference(String rawText, String reason) {

    public UnresolvedReference {
        Objects.requireNonNull(rawText, "rawText");
        Objects.requireNonNull(reason, "reason");
    }
}

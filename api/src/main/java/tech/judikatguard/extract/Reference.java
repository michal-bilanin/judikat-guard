package tech.judikatguard.extract;

import java.util.Map;
import java.util.Objects;
import org.jspecify.annotations.Nullable;

/**
 * One citation-like span found in a paragraph. The extract → resolve seam.
 *
 * <p>Mirrors {@code jg.models.Reference}, minus the three fields the resolver fills in:
 * this record is what the rules pass produces and nothing more. Resolution against
 * {@code decision_alias} happens in the query path, not here.
 *
 * @param kind         which pattern in {@code extract/patterns.toml} matched
 * @param rawText      the <em>whole</em> match including its prefix
 *                     ({@code "sp. zn. 6 Ads 45/2014"}), not the capture group
 * @param start        offset into the paragraph body, inclusive
 * @param end          offset into the paragraph body, exclusive; {@code body.substring(start, end)}
 *                     equals {@code rawText}
 * @param paragraphIdx 1-based, matching {@code decision_paragraph.idx}
 * @param groups       the pattern's named capture groups. A group that did not participate
 *                     is <em>absent</em>, never present as null: an anaphoric
 *                     {@code § 2000 odst. 1} carries no {@code act_no}, and that absence is
 *                     exactly what the resolver's anaphora pass looks for
 */
public record Reference(
        ReferenceKind kind,
        String rawText,
        int start,
        int end,
        int paragraphIdx,
        Map<String, String> groups) {

    public Reference {
        Objects.requireNonNull(kind, "kind");
        Objects.requireNonNull(rawText, "rawText");
        Objects.requireNonNull(groups, "groups");
        if (start < 0) {
            throw new IllegalArgumentException("start must not be negative: " + start);
        }
        if (end < start) {
            throw new IllegalArgumentException("end " + end + " precedes start " + start);
        }
        // Rejects null values as well as freezing the map, which is how "absent" stays the
        // only way to say "this group did not match".
        groups = Map.copyOf(groups);
    }

    /** Length of the matched span in characters. The overlap policy prefers the longest. */
    public int length() {
        return end - start;
    }

    /** One capture group, or null when the group did not participate in the match. */
    public @Nullable String group(String name) {
        return groups.get(name);
    }

    /**
     * True when this span fully contains {@code other} — same paragraph, and the bounds of
     * {@code other} lie inside these bounds. Equal spans contain each other.
     */
    public boolean contains(Reference other) {
        return paragraphIdx == other.paragraphIdx && start <= other.start && end >= other.end;
    }
}

package tech.judikatguard.status;

import java.time.LocalDate;
import java.util.Objects;
import org.jspecify.annotations.Nullable;

/**
 * One statutory provision the evaluated decision relies on, together with what has since
 * happened to it. PLAN.md section 10.
 *
 * <p>{@code reworded}, {@code material} and {@code derogatedBy} are resolved by the
 * repository layer (version-in-force comparison plus, where the text differs, one cached
 * materiality call). The engine only reads them; it never decides materiality itself.
 *
 * @param provisionId  {@code provision.id}
 * @param actNo        e.g. {@code 89/2012}
 * @param section      e.g. {@code 2000}
 * @param subsec       e.g. {@code 1}; null when the citation named no subsection
 * @param reworded     the text in force at {@code asOf} differs from the text in force when
 *                     the evaluated decision was decided
 * @param changedOn    the date the wording changed, or the date the derogation took effect;
 *                     null when nothing has changed
 * @param material     the change touches the reasoning rather than being cosmetic
 * @param derogatedBy  ECLI of the Constitutional Court decision that struck the provision
 *                     down ({@code provision_version.derogated_by}); null in the normal case
 */
public record ProvisionEvidence(
        long provisionId,
        String actNo,
        String section,
        @Nullable String subsec,
        boolean reworded,
        @Nullable LocalDate changedOn,
        boolean material,
        @Nullable String derogatedBy) {

    public ProvisionEvidence {
        Objects.requireNonNull(actNo, "actNo");
        Objects.requireNonNull(section, "section");
    }
}

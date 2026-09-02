package tech.judikatguard.status;

import java.util.Objects;

/**
 * A source the evaluated decision wholly relies on (a {@code FOLLOWED} citation) together
 * with that source's own light, computed in a previous, separate evaluation.
 *
 * <p>Exactly one hop. The engine never recurses: whoever assembles the {@link Evaluation}
 * resolves these lights and hands them in already computed. Deeper propagation is
 * unexplainable and noisy (PLAN.md section 9).
 */
public record InheritedEvidence(String viaEcli, Light light) {

    public InheritedEvidence {
        Objects.requireNonNull(viaEcli, "viaEcli");
        Objects.requireNonNull(light, "light");
    }
}

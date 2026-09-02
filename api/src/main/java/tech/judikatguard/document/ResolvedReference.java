package tech.judikatguard.document;

import java.util.Objects;
import org.jspecify.annotations.Nullable;
import tech.judikatguard.decision.ProvisionRepository.ProvisionKey;
import tech.judikatguard.extract.Reference;

/**
 * One extracted reference after resolution: a decision, a provision, or neither.
 *
 * <p>"Neither" is a first-class outcome. It carries the reason it did not resolve and it is
 * reported to the user, because a tool that silently drops the citations it cannot handle
 * is a tool whose coverage nobody can judge (PLAN.md section 7).
 *
 * @param reference the span the extractor found, with the raw text exactly as the document
 *     wrote it — never reconstructed from the resolved identifiers
 * @param ecli the decision it names, when it resolved to one
 * @param provisionId the {@code provision.id} it names, when it resolved to one
 * @param provisionKey the act/section/subsection actually used, including an act number
 *     filled in by the anaphora pass, so the report can show what was assumed
 * @param unresolvedReason set exactly when neither target resolved
 */
public record ResolvedReference(
        Reference reference,
        @Nullable String ecli,
        @Nullable Long provisionId,
        @Nullable ProvisionKey provisionKey,
        @Nullable String unresolvedReason) {

    public ResolvedReference {
        Objects.requireNonNull(reference, "reference");
    }

    static ResolvedReference decision(Reference reference, String ecli) {
        return new ResolvedReference(reference, ecli, null, null, null);
    }

    static ResolvedReference provision(Reference reference, long provisionId, ProvisionKey key) {
        return new ResolvedReference(reference, null, provisionId, key, null);
    }

    static ResolvedReference unresolved(Reference reference, String reason) {
        return new ResolvedReference(reference, null, null, null, reason);
    }

    /**
     * The citation as the document wrote it. The report shows this and never a form rebuilt
     * from the resolved identifiers: the user has to be able to find it on their own page.
     */
    public String rawText() {
        return reference.rawText();
    }
}

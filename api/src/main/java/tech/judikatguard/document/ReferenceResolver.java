package tech.judikatguard.document;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import org.jspecify.annotations.Nullable;
import org.springframework.stereotype.Component;
import tech.judikatguard.decision.AliasRepository;
import tech.judikatguard.decision.ProvisionRepository;
import tech.judikatguard.decision.ProvisionRepository.ProvisionKey;
import tech.judikatguard.extract.Paragraphs;
import tech.judikatguard.extract.Reference;
import tech.judikatguard.extract.ReferenceKind;

/**
 * Turns extracted references into ECLIs and provision ids. Java twin of
 * {@code jg.extract.resolver} (PLAN.md section 7).
 *
 * <p>Both runtimes must agree on two things or the corpus and the query path will disagree
 * about what a citation means: the alias normalisation rule, and the order candidate
 * aliases are tried in. Both are mirrored here method for method.
 *
 * <p>An unresolved reference is not an error. It is a coverage metric, and it reaches the
 * user as a row of {@link DocumentReport#unresolved()} with a reason.
 */
@Component
public final class ReferenceResolver {

    /** {@code UnresolvedReference.reason} values, matching {@code web/src/czech.ts}. */
    public static final String NOT_IN_CORPUS = "not in corpus";

    public static final String NOT_RESOLVED = "not resolved";

    private final AliasRepository aliases;
    private final ProvisionRepository provisions;

    public ReferenceResolver(AliasRepository aliases, ProvisionRepository provisions) {
        this.aliases = Objects.requireNonNull(aliases, "aliases");
        this.provisions = Objects.requireNonNull(provisions, "provisions");
    }

    /**
     * Resolve one document's references, in document order.
     *
     * <p>Two queries regardless of document size: one over {@code decision_alias}, one over
     * {@code provision}.
     */
    public List<ResolvedReference> resolve(List<Reference> references) {
        Objects.requireNonNull(references, "references");
        Map<Reference, String> actNoByReference = assignProvisionActs(references);

        Set<String> wantedAliases = new LinkedHashSet<>();
        for (Reference reference : references) {
            wantedAliases.addAll(aliasCandidates(reference));
        }
        Map<String, String> ecliByAlias = aliases.resolve(wantedAliases);

        Set<ProvisionKey> wantedKeys = new LinkedHashSet<>();
        for (Reference reference : references) {
            provisionKey(reference, actNoByReference).ifPresent(wantedKeys::add);
        }
        Map<ProvisionKey, Long> idByKey = provisions.resolve(wantedKeys);

        List<ResolvedReference> resolved = new ArrayList<>(references.size());
        for (Reference reference : references) {
            resolved.add(
                    reference.kind() == ReferenceKind.PROVISION
                            ? resolveProvision(reference, actNoByReference, idByKey)
                            : resolveDecision(reference, ecliByAlias));
        }
        return List.copyOf(resolved);
    }

    private static ResolvedReference resolveDecision(
            Reference reference, Map<String, String> ecliByAlias) {
        for (String candidate : aliasCandidates(reference)) {
            String ecli = ecliByAlias.get(candidate);
            if (ecli != null) {
                return ResolvedReference.decision(reference, ecli);
            }
        }
        return ResolvedReference.unresolved(reference, NOT_IN_CORPUS);
    }

    private static ResolvedReference resolveProvision(
            Reference reference,
            Map<Reference, String> actNoByReference,
            Map<ProvisionKey, Long> idByKey) {
        Optional<ProvisionKey> key = provisionKey(reference, actNoByReference);
        if (key.isEmpty()) {
            // No act number anywhere in the document to attach the section to. The citation
            // is real, we simply cannot say which act it belongs to.
            return ResolvedReference.unresolved(reference, NOT_RESOLVED);
        }
        Long id = idByKey.get(key.get());
        return id == null
                ? ResolvedReference.unresolved(reference, NOT_IN_CORPUS)
                : ResolvedReference.provision(reference, id, key.get());
    }

    /**
     * The one normalisation rule for {@code decision_alias.alias}. Mirrors
     * {@code jg.extract.resolver.normalize_alias}: collapse whitespace, strip, fold case.
     * Punctuation is left alone, so {@code 6 Ads 45/2014} and {@code 6 Ads 45/2014-32} stay
     * two distinct aliases — the sheet number is part of the č. j.
     *
     * <p>The whitespace half is delegated to {@link Paragraphs#normalise(String)} rather
     * than written as {@code split("\\s+")}, because Java's {@code \s} is ASCII-only while
     * Python's is Unicode-aware. A č. j. copied out of a court page routinely contains a
     * non-breaking space, and the two runtimes would then compute different aliases for the
     * same citation: the crawler would write one and the query path would look up the other.
     */
    public static String normalizeAlias(String raw) {
        return Paragraphs.normalise(raw).toLowerCase(Locale.ROOT);
    }

    /**
     * Normalised aliases to try for one reference, most specific first. Mirrors
     * {@code jg.extract.resolver.alias_candidates}.
     *
     * <p>A č. j. carrying a sheet number is tried with the sheet first and without it
     * second, because the corpus may hold either form and the fuller one is the better
     * match.
     */
    public static List<String> aliasCandidates(Reference reference) {
        List<String> raw = new ArrayList<>(2);
        switch (reference.kind()) {
            case REF_NO -> {
                String refNo = reference.group("ref_no");
                String sheetNo = reference.group("sheet_no");
                if (refNo != null && sheetNo != null) {
                    raw.add(refNo + "-" + sheetNo);
                }
                if (refNo != null) {
                    raw.add(refNo);
                }
            }
            case CASE_NO -> {
                String caseNo = reference.group("case_no");
                if (caseNo != null) {
                    raw.add(caseNo);
                }
            }
            case ECLI -> raw.add(reference.rawText());
            case JOURNAL_NO -> {
                String seq = reference.group("journal_seq");
                String year = reference.group("journal_year");
                if (seq != null && year != null) {
                    raw.add("R " + seq + "/" + year);
                }
                raw.add(reference.rawText());
            }
            case PROVISION -> {
                // A provision is not a decision and has no alias.
            }
        }
        Set<String> unique = new LinkedHashSet<>();
        for (String value : raw) {
            String normalised = normalizeAlias(value);
            if (!normalised.isEmpty()) {
                unique.add(normalised);
            }
        }
        return List.copyOf(unique);
    }

    /**
     * Provision anaphora, as a pure function over one document's references. Mirrors
     * {@code jg.extract.resolver.assign_provision_acts}.
     *
     * <p>{@code § 2000 odst. 1} usually omits the act. Fill it in from the most recent
     * explicit act number <em>earlier</em> in the document, falling back to the act cited
     * most often in the document. A reference with neither stays unresolved rather than
     * being attached to a guess.
     *
     * @return the act number to use for each provision reference that has one
     */
    static Map<Reference, String> assignProvisionActs(List<Reference> references) {
        List<Reference> provisionRefs = references.stream()
                .filter(reference -> reference.kind() == ReferenceKind.PROVISION)
                .sorted(Comparator.comparingInt(Reference::paragraphIdx)
                        .thenComparingInt(Reference::start)
                        .thenComparingInt(Reference::end))
                .toList();
        if (provisionRefs.isEmpty()) {
            return Map.of();
        }

        Map<String, Integer> explicitCounts = new LinkedHashMap<>();
        for (Reference reference : provisionRefs) {
            String actNo = reference.group("act_no");
            if (actNo != null) {
                explicitCounts.merge(actNo, 1, Integer::sum);
            }
        }
        @Nullable String documentMode = explicitCounts.entrySet().stream()
                .max(Map.Entry.comparingByValue())
                .map(Map.Entry::getKey)
                .orElse(null);

        Map<Reference, String> byReference = new HashMap<>();
        @Nullable String mostRecent = null;
        for (Reference reference : provisionRefs) {
            String explicit = reference.group("act_no");
            if (explicit != null) {
                byReference.put(reference, explicit);
                mostRecent = explicit;
            } else if (mostRecent != null) {
                byReference.put(reference, mostRecent);
            } else if (documentMode != null) {
                byReference.put(reference, documentMode);
            }
        }
        return Map.copyOf(byReference);
    }

    /**
     * {@code (act_no, section, subsec)} for a provision reference, or empty when the act
     * could not be determined. Mirrors {@code jg.extract.resolver.provision_key}.
     */
    static Optional<ProvisionKey> provisionKey(
            Reference reference, Map<Reference, String> actNoByReference) {
        if (reference.kind() != ReferenceKind.PROVISION) {
            return Optional.empty();
        }
        String actNo = actNoByReference.get(reference);
        String section = reference.group("section");
        if (actNo == null || section == null) {
            return Optional.empty();
        }
        return Optional.of(new ProvisionKey(actNo, section, reference.group("subsec")));
    }
}

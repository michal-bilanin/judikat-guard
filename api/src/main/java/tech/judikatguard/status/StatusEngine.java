package tech.judikatguard.status;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Objects;

/**
 * The verdict layer. PLAN.md section 9.
 *
 * <p>A pure function: no repository calls, no clock reads, no I/O, no scoped values, no
 * Spring, no static mutable state. Everything arrives through {@link Evaluation}. That is
 * what lets the truth table below call it directly, and what lets the verdict policy change
 * without re-running a single inference (D2).
 *
 * <h2>Evaluation order</h2>
 *
 * The table is both the light precedence and the order reasons are listed in:
 *
 * <ol>
 *   <li>any {@code QUASHED} &rarr; RED, {@link Reason.Quashed}</li>
 *   <li>any {@code DEPARTED} by a body with departure authority &rarr; RED, {@link Reason.Superseded}</li>
 *   <li>a relied-on provision was derogated &rarr; RED, {@link Reason.ProvisionDerogated}</li>
 *   <li>any {@code DEPARTED} without that authority &rarr; AMBER, {@link Reason.Conflict}</li>
 *   <li>any {@code NARROWED} &rarr; AMBER, {@link Reason.Narrowed}</li>
 *   <li>a relied-on provision was reworded and the change is material &rarr; AMBER, {@link Reason.ProvisionReworded}</li>
 *   <li>any {@code UNCLASSIFIED} &rarr; AMBER, {@link Reason.NeedsReview}</li>
 *   <li>one hop: a source wholly relied on is itself RED &rarr; AMBER, {@link Reason.InheritedWeakness}</li>
 *   <li>otherwise GREEN with no reasons, rendered as "no adverse treatment found"</li>
 * </ol>
 *
 * <p>Rows 1 to 3 produce RED, rows 4 to 8 produce AMBER, and the strongest light wins. The
 * reason list is <em>not</em> truncated at the first match: a RED status still lists its
 * amber-grade reasons after the red ones. They are true facts about the source and the
 * evidence panel shows them; hiding them would make the panel look like it had missed
 * something the user can see for themselves in the citation list.
 *
 * <p>Within one row, reasons are ordered by citing date then citing ECLI so that the panel
 * is stable across runs regardless of the order the repository returned rows in.
 */
public final class StatusEngine {

    /** Stable ordering inside a single row of the table: oldest first, then by ECLI. */
    private static final Comparator<EvidenceRow> BY_DATE_THEN_ECLI =
            Comparator.comparing(EvidenceRow::citingDate).thenComparing(EvidenceRow::citingEcli);

    private StatusEngine() {}

    public static Status evaluate(Evaluation e) {
        Objects.requireNonNull(e, "evaluation");

        // "As of date D" is the whole question, so anything the world learned after D is not
        // evidence for it. Provisions with no recorded change date are never in the future.
        LocalDate asOf = e.asOf();
        List<EvidenceRow> treatments = e.treatments().stream()
                .filter(r -> !r.citingDate().isAfter(asOf))
                .sorted(BY_DATE_THEN_ECLI)
                .toList();
        List<ProvisionEvidence> provisions = e.provisions().stream()
                .filter(p -> p.changedOn() == null || !p.changedOn().isAfter(asOf))
                .sorted(Comparator.comparingLong(ProvisionEvidence::provisionId))
                .toList();
        List<InheritedEvidence> inherited = e.inherited().stream()
                .sorted(Comparator.comparing(InheritedEvidence::viaEcli))
                .toList();

        BigDecimal redBar = e.thresholds().redMinConfidence();
        BigDecimal amberBar = e.thresholds().amberMinConfidence();

        List<Reason> red = new ArrayList<>();
        List<Reason> amber = new ArrayList<>();

        // Row 1: formally annulled.
        for (EvidenceRow r : treatments) {
            if (r.label() == TreatmentLabel.QUASHED && meets(r, redBar)) {
                red.add(new Reason.Quashed(r.citingEcli(), r.evidenceSpan()));
            }
        }
        // Row 2: departed from by a body that may bind. departure_authority decided this,
        // not this class (D5).
        for (EvidenceRow r : treatments) {
            if (r.label() == TreatmentLabel.DEPARTED && r.citingMayDepart() && meets(r, redBar)) {
                red.add(new Reason.Superseded(r.citingEcli(), r.citingPanel(), r.evidenceSpan()));
            }
        }
        // Row 3: the provision itself is gone. No treatment row needed; the provision_version
        // row is the evidence.
        for (ProvisionEvidence p : provisions) {
            String derogatedBy = p.derogatedBy();
            if (derogatedBy != null) {
                red.add(new Reason.ProvisionDerogated(p.provisionId(), derogatedBy));
            }
        }

        // Row 4: departure without the authority to make it stick. Note this is deliberately
        // not a fallback for a row-2 candidate that missed the red bar: a below-threshold row
        // is dropped entirely rather than demoted, because it is not evidence of anything.
        for (EvidenceRow r : treatments) {
            if (r.label() == TreatmentLabel.DEPARTED && !r.citingMayDepart() && meets(r, amberBar)) {
                amber.add(new Reason.Conflict(r.citingEcli(), r.evidenceSpan()));
            }
        }
        // Row 5: scope limited.
        for (EvidenceRow r : treatments) {
            if (r.label() == TreatmentLabel.NARROWED && meets(r, amberBar)) {
                amber.add(new Reason.Narrowed(r.citingEcli(), r.evidenceSpan()));
            }
        }
        // Row 6: the differentiator. A spotless citation history means nothing if the rule
        // being interpreted was rewritten underneath the decision (PLAN.md section 10).
        for (ProvisionEvidence p : provisions) {
            LocalDate changedOn = p.changedOn();
            if (p.reworded() && p.material() && changedOn != null) {
                amber.add(new Reason.ProvisionReworded(p.provisionId(), changedOn, true));
            }
        }
        // Row 7: classification failed validation twice. Exempt from the confidence bars,
        // since an UNCLASSIFIED row's confidence is meaningless by construction.
        for (EvidenceRow r : treatments) {
            if (r.label() == TreatmentLabel.UNCLASSIFIED) {
                amber.add(new Reason.NeedsReview(r.citingEcli(), r.citationId()));
            }
        }
        // Row 8: exactly one hop, supplied pre-computed. This method never recurses.
        for (InheritedEvidence i : inherited) {
            if (i.light() == Light.RED) {
                amber.add(new Reason.InheritedWeakness(i.viaEcli()));
            }
        }

        Light light = !red.isEmpty() ? Light.RED : !amber.isEmpty() ? Light.AMBER : Light.GREEN;
        List<Reason> reasons = new ArrayList<>(red.size() + amber.size());
        reasons.addAll(red);
        reasons.addAll(amber);

        return new Status(e.ecli(), light, asOf, e.corpusSize(), e.corpusThrough(), reasons);
    }

    /** A treatment row counts as evidence only at or above the bar for the light it feeds. */
    private static boolean meets(EvidenceRow row, BigDecimal bar) {
        return row.confidence().compareTo(bar) >= 0;
    }
}

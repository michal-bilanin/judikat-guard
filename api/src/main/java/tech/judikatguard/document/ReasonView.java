package tech.judikatguard.document;

import com.fasterxml.jackson.annotation.JsonInclude;
import java.time.LocalDate;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import org.jspecify.annotations.Nullable;
import tech.judikatguard.status.EvidenceRow;
import tech.judikatguard.status.Reason;

/**
 * One {@link Reason} on the wire, flattened onto {@code { kind, byEcli, span, paragraph }}
 * as in the PLAN.md section 11 example.
 *
 * <p>The variant names are the contract with {@code web/src/types.ts} and
 * {@code web/src/czech.ts}, which map each kind to its Czech phrasing
 * (<em>zrušeno</em>, <em>překonáno</em>, <em>argumentačně oslabeno</em>,
 * <em>zúženo</em>). This record therefore carries no user-facing prose of its own: it
 * carries the identifiers and the verbatim span, and the wording lives in one place.
 *
 * <p>{@code paragraph} is not on {@link Reason} — the engine has no use for it — so it is
 * joined back from the {@code treatment} rows the reason was derived from. That is what
 * makes the evidence panel able to link to a paragraph of a real document.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ReasonView(
        String kind,
        @Nullable String byEcli,
        @Nullable String span,
        @Nullable Integer paragraph,
        @Nullable String panel,
        @Nullable String viaEcli,
        @Nullable Long provisionId,
        @Nullable LocalDate changedOn,
        @Nullable Boolean material) {

    public static final String QUASHED = "QUASHED";
    public static final String SUPERSEDED = "SUPERSEDED";
    public static final String CONFLICT = "CONFLICT";
    public static final String NARROWED = "NARROWED";
    public static final String PROVISION_REWORDED = "PROVISION_REWORDED";
    public static final String PROVISION_DEROGATED = "PROVISION_DEROGATED";
    public static final String INHERITED_WEAKNESS = "INHERITED_WEAKNESS";

    /**
     * {@code Reason.NeedsReview} on the wire. Named for the label that produced it, because
     * that is what the user has to go and look at, and because calling a failed
     * classification <em>argumentačně oslabeno</em> would assert something the evidence does
     * not support.
     */
    public static final String UNCLASSIFIED = "UNCLASSIFIED";

    public ReasonView {
        Objects.requireNonNull(kind, "kind");
    }

    /**
     * Map the engine's reasons onto the wire, joining each back to the treatment row it came
     * from so that the paragraph index travels with it.
     *
     * @param rows the treatment rows the evaluation was computed from
     */
    public static List<ReasonView> of(List<Reason> reasons, List<EvidenceRow> rows) {
        Evidence evidence = Evidence.of(rows);
        return reasons.stream().map(reason -> from(reason, evidence)).toList();
    }

    private static ReasonView from(Reason reason, Evidence evidence) {
        return switch (reason) {
            case Reason.Quashed(String byEcli, String span) ->
                    treatment(QUASHED, byEcli, span, null, evidence);
            case Reason.Superseded(String byEcli, String panel, String span) ->
                    treatment(SUPERSEDED, byEcli, span, panel, evidence);
            case Reason.Conflict(String byEcli, String span) ->
                    treatment(CONFLICT, byEcli, span, null, evidence);
            case Reason.Narrowed(String byEcli, String span) ->
                    treatment(NARROWED, byEcli, span, null, evidence);
            case Reason.NeedsReview(String byEcli, long citationId) -> {
                @Nullable EvidenceRow row = evidence.byCitationId().get(citationId);
                yield new ReasonView(
                        UNCLASSIFIED,
                        byEcli,
                        row == null ? null : row.evidenceSpan(),
                        row == null ? null : row.paragraphIdx(),
                        null,
                        null,
                        null,
                        null,
                        null);
            }
            case Reason.ProvisionReworded(long provisionId, LocalDate changedOn, boolean material) ->
                    new ReasonView(
                            PROVISION_REWORDED, null, null, null, null, null,
                            provisionId, changedOn, material);
            case Reason.ProvisionDerogated(long provisionId, String byEcli) ->
                    new ReasonView(
                            PROVISION_DEROGATED, byEcli, null, null, null, null,
                            provisionId, null, null);
            case Reason.InheritedWeakness(String viaEcli) ->
                    new ReasonView(
                            INHERITED_WEAKNESS, null, null, null, null, viaEcli, null, null, null);
        };
    }

    private static ReasonView treatment(
            String kind,
            String byEcli,
            String span,
            @Nullable String panel,
            Evidence evidence) {
        @Nullable EvidenceRow row = evidence.byCitingAndSpan().get(new Key(byEcli, span));
        return new ReasonView(
                kind, byEcli, span, row == null ? null : row.paragraphIdx(), panel,
                null, null, null, null);
    }

    /** Lookups from a reason back to the row it was derived from. */
    private record Evidence(
            Map<Key, EvidenceRow> byCitingAndSpan, Map<Long, EvidenceRow> byCitationId) {

        static Evidence of(List<EvidenceRow> rows) {
            // Merge keeps the first row: two treatment rows quoting the identical span from
            // the same decision are the same finding, and the earlier one is the one the
            // engine listed.
            Map<Key, EvidenceRow> bySpan = new LinkedHashMap<>();
            Map<Long, EvidenceRow> byId = new LinkedHashMap<>();
            for (EvidenceRow row : rows) {
                bySpan.putIfAbsent(new Key(row.citingEcli(), row.evidenceSpan()), row);
                byId.putIfAbsent(row.citationId(), row);
            }
            return new Evidence(Map.copyOf(bySpan), Map.copyOf(byId));
        }
    }

    private record Key(String citingEcli, String span) {}
}

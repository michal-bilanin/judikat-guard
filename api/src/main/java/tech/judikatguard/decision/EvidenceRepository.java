package tech.judikatguard.decision;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;
import tech.judikatguard.status.EvidenceRow;
import tech.judikatguard.status.TreatmentLabel;

/**
 * The citation graph: who treated a decision how, and whether they had the authority to.
 *
 * <p>This is where the interesting SQL lives. Two queries, both keyed on a batch of ECLIs
 * so that a whole uploaded document costs a fixed number of round trips.
 */
@Repository
public final class EvidenceRepository {

    /**
     * Every classified citation pointing <em>at</em> the given decisions.
     *
     * <p>The {@code left join departure_authority} on (citing court, citing panel type,
     * cited court) is the reason D5 puts the legal authority rules in a table: whether an
     * NSS panel may depart from NSS case law is a fact about Czech procedure, and it belongs
     * in seeded data that a lawyer can read, not in a Java conditional. A missing row means
     * {@code false} — an unrecognised court/panel pair cannot bind, so it degrades to an
     * amber conflict rather than to a red supersession.
     *
     * <p>{@code distinct on (c.id)} keeps one treatment per citation. {@code treatment} is
     * unique on {@code (citation_id, prompt_version)}, so a re-classification under a newer
     * prompt leaves both rows in place; the newest {@code created_at} wins, and the older
     * label stays in the table as history rather than being counted twice.
     */
    private static final String TREATMENTS_SQL = """
            select distinct on (c.id)
                   c.id              as citation_id,
                   c.cited_ecli      as cited_ecli,
                   c.paragraph_idx   as paragraph_idx,
                   citing.ecli       as citing_ecli,
                   citing.court_code as citing_court,
                   citing.panel_type as citing_panel,
                   citing.decided_on as citing_date,
                   t.label           as label,
                   t.confidence      as confidence,
                   t.evidence_span   as evidence_span,
                   coalesce(da.binding, false) as citing_may_depart
              from citation c
              join decision cited  on cited.ecli  = c.cited_ecli
              join decision citing on citing.ecli = c.citing_ecli
              join treatment t     on t.citation_id = c.id
              left join departure_authority da
                     on da.citing_court = citing.court_code
                    and da.citing_panel = citing.panel_type
                    and da.cited_court  = cited.court_code
             where c.cited_ecli in (:eclis)
             order by c.id, t.created_at desc
            """;

    /**
     * The sources each given decision wholly relies on: its {@code FOLLOWED} citations.
     *
     * <p><b>Exactly one hop.</b> This returns the immediate sources and stops. A recursive
     * CTE would work as well, but it would have to be written with a depth guard, and the
     * flat join makes the depth impossible to get wrong. Deeper propagation is
     * unexplainable and noisy (PLAN.md section 9), and an unexplainable amber is worse than
     * no amber.
     *
     * <p>Self-citations are excluded: a decision that cites itself would otherwise inherit
     * its own light and turn every red into a red plus a spurious inherited amber.
     */
    private static final String FOLLOWED_SQL = """
            with latest as (
                select distinct on (c.id)
                       c.citing_ecli as citing_ecli,
                       c.cited_ecli  as cited_ecli,
                       t.label       as label
                  from citation c
                  join treatment t on t.citation_id = c.id
                 where c.citing_ecli in (:eclis)
                   and c.cited_ecli is not null
                   and c.cited_ecli <> c.citing_ecli
                 order by c.id, t.created_at desc
            )
            select distinct citing_ecli, cited_ecli
              from latest
             where label = 'FOLLOWED'
             order by citing_ecli, cited_ecli
            """;

    private final JdbcClient jdbc;

    public EvidenceRepository(JdbcClient jdbc) {
        this.jdbc = Objects.requireNonNull(jdbc, "jdbc");
    }

    /** Treatment rows grouped by the ECLI they point at. Absent means no adverse treatment. */
    public Map<String, List<EvidenceRow>> treatmentsFor(Collection<String> citedEclis) {
        if (citedEclis.isEmpty()) {
            return Map.of();
        }
        List<Targeted> rows = jdbc.sql(TREATMENTS_SQL)
                .param("eclis", List.copyOf(citedEclis))
                .query((rs, rowNum) -> new Targeted(
                        rs.getString("cited_ecli"),
                        new EvidenceRow(
                                rs.getLong("citation_id"),
                                rs.getString("citing_ecli"),
                                rs.getString("citing_court"),
                                rs.getString("citing_panel"),
                                rs.getObject("citing_date", LocalDate.class),
                                label(rs.getString("label")),
                                rs.getBigDecimal("confidence"),
                                rs.getString("evidence_span"),
                                rs.getObject("paragraph_idx", Integer.class),
                                rs.getBoolean("citing_may_depart"))))
                .list();

        Map<String, List<EvidenceRow>> byCited = new LinkedHashMap<>();
        for (Targeted row : rows) {
            byCited.computeIfAbsent(row.citedEcli(), key -> new ArrayList<>()).add(row.evidence());
        }
        return Map.copyOf(byCited);
    }

    /** One treatment row plus the ECLI it points at, the key the caller groups on. */
    private record Targeted(String citedEcli, EvidenceRow evidence) {}

    /** One hop: for each given ECLI, the ECLIs of the sources it {@code FOLLOWED}. */
    public Map<String, List<String>> whollyReliedOn(Collection<String> eclis) {
        if (eclis.isEmpty()) {
            return Map.of();
        }
        List<Edge> edges = jdbc.sql(FOLLOWED_SQL)
                .param("eclis", List.copyOf(eclis))
                .query((rs, rowNum) ->
                        new Edge(rs.getString("citing_ecli"), rs.getString("cited_ecli")))
                .list();

        Map<String, List<String>> byCiting = new LinkedHashMap<>();
        for (Edge edge : edges) {
            byCiting.computeIfAbsent(edge.citingEcli(), key -> new ArrayList<>()).add(edge.citedEcli());
        }
        return Map.copyOf(byCiting);
    }

    /** One {@code FOLLOWED} edge of the graph, in the direction the citation points. */
    private record Edge(String citingEcli, String citedEcli) {}

    /**
     * A label the pipeline wrote that this build does not know becomes
     * {@code UNCLASSIFIED}: amber "needs review". Guessing a substantive label from an
     * unknown string is how a false red gets into a demo.
     */
    private static TreatmentLabel label(String stored) {
        try {
            return TreatmentLabel.valueOf(stored);
        } catch (IllegalArgumentException e) {
            return TreatmentLabel.UNCLASSIFIED;
        }
    }
}

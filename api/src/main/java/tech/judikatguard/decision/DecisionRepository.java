package tech.judikatguard.decision;

import java.time.LocalDate;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/** Reads {@code decision} and {@code decision_paragraph}. */
@Repository
public final class DecisionRepository {

    private static final String COLUMNS = """
            ecli, court_code, panel_type, decided_on, case_no, ref_no, journal_no,
            ratio_summary, source_url
            """;

    private static final String FIND_SQL =
            "select " + COLUMNS + " from decision where ecli = :ecli";

    private static final String FIND_ALL_SQL =
            "select " + COLUMNS + " from decision where ecli in (:eclis)";

    private static final String PARAGRAPHS_SQL = """
            select body
              from decision_paragraph
             where ecli = :ecli
             order by idx
            """;

    private static final RowMapper<DecisionSummary> MAPPER = (rs, rowNum) -> new DecisionSummary(
            rs.getString("ecli"),
            rs.getString("court_code"),
            rs.getString("panel_type"),
            rs.getObject("decided_on", LocalDate.class),
            rs.getString("case_no"),
            rs.getString("ref_no"),
            rs.getString("journal_no"),
            rs.getString("ratio_summary"),
            rs.getString("source_url"));

    private final JdbcClient jdbc;

    public DecisionRepository(JdbcClient jdbc) {
        this.jdbc = Objects.requireNonNull(jdbc, "jdbc");
    }

    /** Empty when the ECLI is not in the corpus, which is a normal state, not an error. */
    public Optional<DecisionSummary> find(String ecli) {
        return jdbc.sql(FIND_SQL).param("ecli", ecli).query(MAPPER).optional();
    }

    /**
     * Batch form, so that a whole uploaded document costs one query. Missing ECLIs are
     * simply absent from the returned map.
     */
    public Map<String, DecisionSummary> findAll(Collection<String> eclis) {
        if (eclis.isEmpty()) {
            return Map.of();
        }
        Map<String, DecisionSummary> byEcli = new LinkedHashMap<>();
        for (DecisionSummary summary :
                jdbc.sql(FIND_ALL_SQL).param("eclis", List.copyOf(eclis)).query(MAPPER).list()) {
            byEcli.put(summary.ecli(), summary);
        }
        return Map.copyOf(byEcli);
    }

    /**
     * The decision's paragraphs in order. This is the material the proposition check quotes
     * from, so an {@code evidence_span} can only ever be verbatim text that is really in the
     * corpus (PLAN.md section 12).
     */
    public List<String> paragraphs(String ecli) {
        return jdbc.sql(PARAGRAPHS_SQL).param("ecli", ecli).query(String.class).list();
    }
}

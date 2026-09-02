package tech.judikatguard.decision;

import java.time.LocalDate;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/**
 * {@code corpus_meta}: the scope clause of every answer this system gives.
 *
 * <p>These two numbers are what turns "this source is fine" into "no adverse treatment was
 * found in N decisions published through date C" (PLAN.md section 2). They are therefore
 * read on the same request as the verdict, not cached at startup, so a crawl that finishes
 * mid-demo widens the stated scope immediately.
 */
@Repository
public final class CorpusRepository {

    private static final String COVERAGE_SQL = """
            select court_code, decision_count, covered_through
              from corpus_meta
             order by court_code
            """;

    private final JdbcClient jdbc;

    public CorpusRepository(JdbcClient jdbc) {
        this.jdbc = Objects.requireNonNull(jdbc, "jdbc");
    }

    public Corpus coverage() {
        Map<String, CorpusCoverage> courts = new LinkedHashMap<>();
        jdbc.sql(COVERAGE_SQL)
                .query((rs, rowNum) -> Map.entry(
                        rs.getString("court_code"),
                        new CorpusCoverage(
                                rs.getInt("decision_count"),
                                rs.getObject("covered_through", LocalDate.class))))
                .list()
                .forEach(entry -> courts.put(entry.getKey(), entry.getValue()));
        return Corpus.of(courts);
    }
}

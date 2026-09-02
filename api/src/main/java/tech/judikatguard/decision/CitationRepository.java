package tech.judikatguard.decision;

import java.util.Objects;
import java.util.Optional;
import org.jspecify.annotations.Nullable;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/**
 * Reads single {@code citation} rows. Used by the proposition check, which is asked about
 * one citation of the crawled corpus by id.
 *
 * <p>Read-only on purpose. {@code citation.citing_ecli} is {@code not null}, so a citation
 * only exists as part of a crawled decision; a reference found in an uploaded document has
 * no place in this table and none is ever inserted (PLAN.md section 11).
 */
@Repository
public final class CitationRepository {

    private static final String FIND_SQL = """
            select c.id, c.citing_ecli, c.cited_ecli, c.cited_provision, c.paragraph_idx,
                   c.raw_text
              from citation c
             where c.id = :id
            """;

    private final JdbcClient jdbc;

    public CitationRepository(JdbcClient jdbc) {
        this.jdbc = Objects.requireNonNull(jdbc, "jdbc");
    }

    public Optional<CitationRow> find(long id) {
        return jdbc.sql(FIND_SQL)
                .param("id", id)
                .query((rs, rowNum) -> new CitationRow(
                        rs.getLong("id"),
                        rs.getString("citing_ecli"),
                        rs.getString("cited_ecli"),
                        rs.getObject("cited_provision", Long.class),
                        rs.getObject("paragraph_idx", Integer.class),
                        rs.getString("raw_text")))
                .optional();
    }

    /**
     * One row of {@code citation}.
     *
     * @param citedEcli null when the citation targets a provision instead of a decision;
     *     the check constraint guarantees one of the two is present
     * @param paragraphIdx null when the extractor could not place the citation
     */
    public record CitationRow(
            long id,
            String citingEcli,
            @Nullable String citedEcli,
            @Nullable Long citedProvision,
            @Nullable Integer paragraphIdx,
            String rawText) {

        public CitationRow {
            Objects.requireNonNull(citingEcli, "citingEcli");
            Objects.requireNonNull(rawText, "rawText");
        }
    }
}

package tech.judikatguard.decision;

import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/**
 * {@code decision_alias}: every case number, reference number, journal number and ECLI
 * variant that names a decision in the corpus.
 *
 * <p>Aliases are stored already normalised, by the rule in
 * {@code jg.extract.resolver.normalize_alias}. Both sides of this lookup must apply the
 * same rule, so the Java side of it lives in one place
 * ({@code tech.judikatguard.document.ReferenceResolver#normalizeAlias}) and this repository
 * takes strings that have already been through it.
 */
@Repository
public final class AliasRepository {

    private static final String RESOLVE_SQL = """
            select alias, ecli
              from decision_alias
             where alias in (:aliases)
            """;

    private final JdbcClient jdbc;

    public AliasRepository(JdbcClient jdbc) {
        this.jdbc = Objects.requireNonNull(jdbc, "jdbc");
    }

    /** Normalised alias to ECLI, for the aliases that are in the corpus. */
    public Map<String, String> resolve(Collection<String> normalisedAliases) {
        if (normalisedAliases.isEmpty()) {
            return Map.of();
        }
        Map<String, String> byAlias = new LinkedHashMap<>();
        jdbc.sql(RESOLVE_SQL)
                .param("aliases", List.copyOf(normalisedAliases))
                .query((rs, rowNum) -> Map.entry(rs.getString("alias"), rs.getString("ecli")))
                .list()
                .forEach(entry -> byAlias.put(entry.getKey(), entry.getValue()));
        return Map.copyOf(byAlias);
    }
}

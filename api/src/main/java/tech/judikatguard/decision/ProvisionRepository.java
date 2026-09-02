package tech.judikatguard.decision;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import org.jspecify.annotations.Nullable;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;
import tech.judikatguard.status.ProvisionEvidence;

/**
 * The provision layer: what has happened to the statutory rules a decision relies on.
 * PLAN.md section 10, the differentiator.
 *
 * <p>A decision from 2013 interpreting a rule reworded in 2017 can have a spotless citation
 * history and be worthless, and no keyword system finds that, because nothing was ever said
 * against the decision. What finds it is comparing the version in force when the decision
 * was handed down against the version in force on the date the user is asking about.
 */
@Repository
public final class ProvisionRepository {

    /**
     * The version in force on a given date: the latest one whose validity window contains
     * it. Written once as a fragment because it is needed twice per row, at
     * {@code decided_on} and at {@code asOf}, and the two must be the same rule.
     */
    private static final String VERSION_IN_FORCE = """
            select pv.id, pv.body, pv.valid_from
              from provision_version pv
             where pv.provision_id = p.id
               and pv.valid_from <= %s
               and (pv.valid_to is null or pv.valid_to >= %s)
             order by pv.valid_from desc
             limit 1
            """;

    /**
     * Any version of the provision that the Constitutional Court struck down on or before
     * the date asked about. Read separately from the version in force because a derogation
     * usually closes the version's validity window, so at {@code asOf} there is often no
     * version in force at all and the derogation would otherwise vanish exactly when it
     * matters most.
     *
     * <p>The date compared against {@code asOf} is the <em>derogating decision's</em>
     * {@code decided_on}, not the version's {@code valid_from}. Those are years apart: a
     * version in force from 2010 and struck down in 2027 must not read as derogated when the
     * question is "as of 2015". Filtering on {@code valid_from} would answer a different
     * question and would do it in the worst direction, since a false red is the one error D8
     * says destroys trust permanently.
     *
     * <p>The join is inner rather than outer because {@code provision_version.derogated_by}
     * is a foreign key onto {@code decision(ecli)} whose {@code decided_on} is not null, so
     * a derogation always has a dated decision behind it.
     */
    private static final String DEROGATION = """
            select pv.derogated_by,
                   dd.decided_on as derogated_on
              from provision_version pv
              join decision dd on dd.ecli = pv.derogated_by
             where pv.provision_id = p.id
               and pv.derogated_by is not null
               and dd.decided_on <= :asOf
             order by dd.decided_on desc
             limit 1
            """;

    /**
     * Provisions the given decisions rely on, with the comparison already resolved.
     *
     * <p>{@code reworded} is "the bodies differ", nothing cleverer: a diff is shown to the
     * user rather than interpreted here. {@code material} comes from
     * {@code provision_materiality} and defaults to <b>false</b> when no judgement has been
     * made — an un-judged rewording must never produce an amber, because that would be a
     * verdict with no evidence row behind it (CLAUDE.md rule 2).
     */
    private static final String RELIED_ON_SQL = ("""
            select distinct on (c.citing_ecli, p.id)
                   c.citing_ecli as ecli,
                   p.id          as provision_id,
                   p.act_no      as act_no,
                   p.section     as section,
                   p.subsec      as subsec,
                   (v0.id is not null and v1.id is not null and v0.body <> v1.body) as reworded,
                   case when v0.id is distinct from v1.id then v1.valid_from end     as changed_on,
                   coalesce(m.material, false) as material,
                   der.derogated_by            as derogated_by
              from citation c
              join decision d  on d.ecli = c.citing_ecli
              join provision p on p.id = c.cited_provision
              left join lateral (
            %s
              ) v0 on true
              left join lateral (
            %s
              ) v1 on true
              left join lateral (
                  select pm.material
                    from provision_materiality pm
                   where pm.from_version_id = v0.id
                     and pm.to_version_id = v1.id
                   order by pm.created_at desc
                   limit 1
              ) m on true
              left join lateral (
            %s
              ) der on true
             where c.citing_ecli in (:eclis)
               and c.cited_provision is not null
             order by c.citing_ecli, p.id
            """)
            .formatted(
                    VERSION_IN_FORCE.formatted("d.decided_on", "d.decided_on"),
                    VERSION_IN_FORCE.formatted(":asOf", ":asOf"),
                    DEROGATION);

    /**
     * A provision cited directly in an uploaded document, rather than by a decision.
     *
     * <p>Only the derogation is reported. A rewording is a statement <em>relative to a
     * date</em> — the date the reasoning relying on the rule was written — and an uploaded
     * document supplies no such date. Reporting a rewording against an invented baseline
     * would be a verdict the evidence does not carry, so the rewording amber appears on the
     * rows of the decisions that rely on the provision, which is exactly the framing of
     * PLAN.md section 10.
     */
    private static final String DIRECT_SQL = ("""
            select p.id      as provision_id,
                   p.act_no  as act_no,
                   p.section as section,
                   p.subsec  as subsec,
                   der.derogated_by as derogated_by
              from provision p
              left join lateral (
            %s
              ) der on true
             where p.id in (:ids)
             order by p.id
            """).formatted(DEROGATION);

    /**
     * Resolves {@code (act_no, section, subsec)} keys. Filtered on the first two columns
     * only, which the unique index covers; the caller matches the subsection, so that a
     * citation naming no subsection does not accidentally match one that does.
     */
    private static final String LOOKUP_SQL = """
            select id, act_no, section, subsec
              from provision
             where act_no in (:actNos)
               and section in (:sections)
            """;

    private final JdbcClient jdbc;

    public ProvisionRepository(JdbcClient jdbc) {
        this.jdbc = Objects.requireNonNull(jdbc, "jdbc");
    }

    /** Provision evidence grouped by the ECLI of the decision that relies on it. */
    public Map<String, List<ProvisionEvidence>> reliedOn(
            Collection<String> eclis, LocalDate asOf) {
        if (eclis.isEmpty()) {
            return Map.of();
        }
        List<Relied> rows = jdbc.sql(RELIED_ON_SQL)
                .param("eclis", List.copyOf(eclis))
                .param("asOf", asOf)
                .query((rs, rowNum) -> new Relied(
                        rs.getString("ecli"),
                        new ProvisionEvidence(
                                rs.getLong("provision_id"),
                                rs.getString("act_no"),
                                rs.getString("section"),
                                rs.getString("subsec"),
                                rs.getBoolean("reworded"),
                                rs.getObject("changed_on", LocalDate.class),
                                rs.getBoolean("material"),
                                rs.getString("derogated_by"))))
                .list();

        Map<String, List<ProvisionEvidence>> byEcli = new LinkedHashMap<>();
        for (Relied row : rows) {
            byEcli.computeIfAbsent(row.ecli(), key -> new ArrayList<>()).add(row.provision());
        }
        return Map.copyOf(byEcli);
    }

    /** Derogation evidence for provisions cited directly in an uploaded document. */
    public Map<Long, ProvisionEvidence> directlyCited(Collection<Long> ids, LocalDate asOf) {
        if (ids.isEmpty()) {
            return Map.of();
        }
        Map<Long, ProvisionEvidence> byId = new LinkedHashMap<>();
        for (ProvisionEvidence evidence : jdbc.sql(DIRECT_SQL)
                .param("ids", List.copyOf(ids))
                .param("asOf", asOf)
                .query((rs, rowNum) -> new ProvisionEvidence(
                        rs.getLong("provision_id"),
                        rs.getString("act_no"),
                        rs.getString("section"),
                        rs.getString("subsec"),
                        false,
                        null,
                        false,
                        rs.getString("derogated_by")))
                .list()) {
            byId.put(evidence.provisionId(), evidence);
        }
        return Map.copyOf(byId);
    }

    /**
     * Resolves provision citations to {@code provision.id}. Read-only: rows are created by
     * the e-Sbírka ingest, never by the query path.
     *
     * @param keys {@code (act_no, section, subsec)}, subsection null when none was named
     */
    public Map<ProvisionKey, Long> resolve(Collection<ProvisionKey> keys) {
        if (keys.isEmpty()) {
            return Map.of();
        }
        List<String> actNos = keys.stream().map(ProvisionKey::actNo).distinct().toList();
        List<String> sections = keys.stream().map(ProvisionKey::section).distinct().toList();

        Map<ProvisionKey, Long> byKey = new LinkedHashMap<>();
        jdbc.sql(LOOKUP_SQL)
                .param("actNos", actNos)
                .param("sections", sections)
                .query((rs, rowNum) -> {
                    ProvisionKey key = new ProvisionKey(
                            rs.getString("act_no"), rs.getString("section"), rs.getString("subsec"));
                    return Map.entry(key, rs.getLong("id"));
                })
                .list()
                .forEach(entry -> byKey.put(entry.getKey(), entry.getValue()));
        return Map.copyOf(byKey);
    }

    /**
     * The natural key of a provision. Mirrors {@code jg.extract.resolver.provision_key}.
     *
     * @param subsec null when the citation named no subsection, which is a different
     *     provision row from one that did
     */
    public record ProvisionKey(String actNo, String section, @Nullable String subsec) {

        public ProvisionKey {
            Objects.requireNonNull(actNo, "actNo");
            Objects.requireNonNull(section, "section");
        }
    }

    /** One provision plus the ECLI of the decision relying on it. */
    private record Relied(String ecli, ProvisionEvidence provision) {}
}

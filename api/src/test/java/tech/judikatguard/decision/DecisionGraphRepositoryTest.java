package tech.judikatguard.decision;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import org.jspecify.annotations.Nullable;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.simple.JdbcClient;
import tech.judikatguard.document.EvaluationContext;
import tech.judikatguard.document.StatusService;
import tech.judikatguard.status.EvidenceRow;
import tech.judikatguard.status.Light;
import tech.judikatguard.status.ProvisionEvidence;
import tech.judikatguard.status.Reason;
import tech.judikatguard.status.Status;

/**
 * The graph queries against a real Postgres with the real migrations applied.
 *
 * <p>Two things are worth a container rather than a mock. The
 * {@code departure_authority} join is the whole of D5 — whether an NSS panel may depart from
 * NSS case law is seeded data, and a test that stubbed the join would be testing nothing.
 * And the one-hop rule is a property of the queries plus the assembly order, not of the
 * engine, so it can only be shown end to end.
 *
 * <p>Every identifier in the fixtures is prefixed {@code TEST-} and cannot resolve against
 * the real corpus, except the court codes (which are seeded by {@code V3}), the act number
 * {@code 89/2012} and the section {@code 2000}, which are taken from PLAN.md. Nothing here
 * is an invented case number (CLAUDE.md hard rule 1).
 */
class DecisionGraphRepositoryTest extends PostgresBackedTest {

    private static final String CITED = "TEST-ECLI-NSS-CITED";
    private static final String EXTENDED = "TEST-ECLI-NSS-EXTENDED";
    private static final String PANEL = "TEST-ECLI-NSS-PANEL";
    private static final String SPAN_EXTENDED =
            "TEST- Rozšířený senát na dosavadním výkladu nesetrvává.";
    private static final String SPAN_PANEL =
            "TEST- Senát vyslovuje odlišný právní názor.";

    private static final LocalDate AS_OF = LocalDate.parse("2026-09-01");

    @Autowired JdbcClient jdbc;
    @Autowired DecisionRepository decisions;
    @Autowired EvidenceRepository evidence;
    @Autowired ProvisionRepository provisions;
    @Autowired CorpusRepository corpus;
    @Autowired StatusService statuses;

    @BeforeEach
    void reset() {
        jdbc.sql("delete from provision_materiality").update();
        jdbc.sql("delete from treatment").update();
        jdbc.sql("delete from citation").update();
        jdbc.sql("delete from provision_version").update();
        jdbc.sql("delete from provision").update();
        jdbc.sql("delete from decision_paragraph").update();
        jdbc.sql("delete from decision_alias").update();
        jdbc.sql("delete from corpus_meta").update();
        jdbc.sql("delete from decision").update();
        insertCorpusMeta("NSS", 2, LocalDate.parse("2026-08-15"));
    }

    @Nested
    @DisplayName("departure_authority join")
    class DepartureAuthority {

        @Test
        @DisplayName("an extended panel of the same court may depart; a plain panel may not")
        void fillsCitingMayDepart() {
            insertDecision(CITED, "NSS", "panel", "2015-03-12");
            insertDecision(EXTENDED, "NSS", "extended", "2018-06-01");
            insertDecision(PANEL, "NSS", "panel", "2019-01-15");
            departed(EXTENDED, CITED, SPAN_EXTENDED, 41);
            departed(PANEL, CITED, SPAN_PANEL, 12);

            List<EvidenceRow> rows =
                    evidence.treatmentsFor(List.of(CITED)).getOrDefault(CITED, List.of());

            assertThat(rows).hasSize(2);
            assertThat(row(rows, EXTENDED).citingMayDepart())
                    .as("NSS extended over NSS is binding in departure_authority")
                    .isTrue();
            assertThat(row(rows, PANEL).citingMayDepart())
                    .as("NSS panel over NSS is not binding; it must refer instead")
                    .isFalse();
        }

        @Test
        @DisplayName("binding departure is red 'překonáno', non-binding is amber 'oslabeno'")
        void drivesTheLight() {
            insertDecision(CITED, "NSS", "panel", "2015-03-12");
            insertDecision(EXTENDED, "NSS", "extended", "2018-06-01");
            insertDecision(PANEL, "NSS", "panel", "2019-01-15");
            departed(EXTENDED, CITED, SPAN_EXTENDED, 41);
            departed(PANEL, CITED, SPAN_PANEL, 12);

            Status status = evaluate(CITED);

            assertThat(status.light()).isEqualTo(Light.RED);
            assertThat(status.reasons())
                    .containsExactly(
                            new Reason.Superseded(EXTENDED, "extended", SPAN_EXTENDED),
                            new Reason.Conflict(PANEL, SPAN_PANEL));
            assertThat(status.corpusSize()).isEqualTo(2);
            assertThat(status.corpusThrough()).isEqualTo(LocalDate.parse("2026-08-15"));
        }

        @Test
        @DisplayName("an unknown court/panel pair cannot bind, so it degrades to amber")
        void missingRowMeansFalse() {
            insertDecision(CITED, "NSS", "panel", "2015-03-12");
            insertDecision("TEST-ECLI-NSS-UNKNOWN-PANEL", "NSS", "unknown", "2020-04-04");
            departed("TEST-ECLI-NSS-UNKNOWN-PANEL", CITED, "TEST- Odchyluje se.", 3);

            assertThat(evaluate(CITED).light()).isEqualTo(Light.AMBER);
        }
    }

    @Nested
    @DisplayName("one-hop propagation")
    class OneHop {

        private static final String A = "TEST-ECLI-NSS-A";
        private static final String B = "TEST-ECLI-NSS-B";
        private static final String C = "TEST-ECLI-NSS-C";
        private static final String QUASHER = "TEST-ECLI-US-QUASHER";
        private static final String SPAN_QUASHED = "TEST- Rozsudek se ruší.";

        @BeforeEach
        void chain() {
            insertDecision(A, "NSS", "panel", "2020-05-05");
            insertDecision(B, "NSS", "panel", "2016-04-04");
            insertDecision(C, "NSS", "panel", "2010-01-05");
            insertDecision(QUASHER, "US", "panel", "2012-02-02");
            followed(A, B, "TEST- Zdejší soud vychází ze závěrů B.", 7);
            followed(B, C, "TEST- Zdejší soud vychází ze závěrů C.", 9);
            quashed(QUASHER, C, SPAN_QUASHED, 2);
        }

        @Test
        @DisplayName("the repository returns the immediate FOLLOWED sources only")
        void stopsAtTheFirstHop() {
            assertThat(evidence.whollyReliedOn(List.of(A))).isEqualTo(Map.of(A, List.of(B)));
            assertThat(evidence.whollyReliedOn(List.of(B))).isEqualTo(Map.of(B, List.of(C)));
        }

        @Test
        @DisplayName("weakness reaches the direct source and no further")
        void weaknessDoesNotPropagateTwice() {
            assertThat(evaluate(C).light()).as("annulled by name").isEqualTo(Light.RED);

            Status b = evaluate(B);
            assertThat(b.light()).as("relies wholly on a red source").isEqualTo(Light.AMBER);
            assertThat(b.reasons()).containsExactly(new Reason.InheritedWeakness(C));

            Status a = evaluate(A);
            assertThat(a.light())
                    .as("two hops away from the red source, so nothing is inherited")
                    .isEqualTo(Light.GREEN);
            assertThat(a.reasons()).isEmpty();
        }
    }

    @Nested
    @DisplayName("provision versions")
    class Provisions {

        private static final String RELIES = "TEST-ECLI-NSS-RELIES";
        private static final String DEROGATOR = "TEST-ECLI-US-DEROGATOR";
        private static final String OLD_BODY = "TEST- Znění účinné v době rozhodnutí.";
        private static final String NEW_BODY = "TEST- Znění účinné po novele.";

        private long provisionId;
        private long oldVersionId;
        private long newVersionId;

        @BeforeEach
        void reworded() {
            insertDecision(RELIES, "NSS", "panel", "2015-06-01");
            // Act number and section taken from PLAN.md section 6, not invented.
            provisionId = insertProvision("89/2012", "2000", "1");
            oldVersionId = insertVersion(provisionId, OLD_BODY, "2014-01-01", "2016-12-31");
            newVersionId = insertVersion(provisionId, NEW_BODY, "2017-01-01", null);
            citesProvision(RELIES, provisionId, 18);
        }

        @Test
        @DisplayName("the version in force then and now are compared, materiality defaults to false")
        void rewordedButNotYetJudged() {
            ProvisionEvidence provision = onlyProvision();

            assertThat(provision.reworded()).isTrue();
            assertThat(provision.changedOn()).isEqualTo(LocalDate.parse("2017-01-01"));
            assertThat(provision.material())
                    .as("no judgement has been made, so no amber may be claimed")
                    .isFalse();
            assertThat(evaluate(RELIES).light()).isEqualTo(Light.GREEN);
        }

        @Test
        @DisplayName("a material rewording is amber, keyed on the version pair")
        void materialRewordingIsAmber() {
            insertMateriality(oldVersionId, newVersionId, true);

            assertThat(onlyProvision().material()).isTrue();
            Status status = evaluate(RELIES);
            assertThat(status.light()).isEqualTo(Light.AMBER);
            assertThat(status.reasons())
                    .containsExactly(new Reason.ProvisionReworded(
                            provisionId, LocalDate.parse("2017-01-01"), true));
        }

        @Test
        @DisplayName("a derogated provision is red, on the provision_version row alone")
        void derogationIsRed() {
            insertDecision(DEROGATOR, "US", "plenary", "2018-03-03");
            jdbc.sql("update provision_version set derogated_by = :ecli where id = :id")
                    .param("ecli", DEROGATOR)
                    .param("id", newVersionId)
                    .update();

            Status status = evaluate(RELIES);

            assertThat(status.light()).isEqualTo(Light.RED);
            assertThat(status.reasons())
                    .contains(new Reason.ProvisionDerogated(provisionId, DEROGATOR));
        }

        @Test
        @DisplayName("a derogation is dated by the annulling decision, not by the version's valid_from")
        void derogationIsDatedByTheAnnullingDecision() {
            // The newer version has been in force since 2017-01-01. The plénum struck it down
            // in 2020. Those are three years apart, and the question is always "as of date D".
            insertDecision(DEROGATOR, "US", "plenary", "2020-09-09");
            jdbc.sql("update provision_version set derogated_by = :ecli where id = :id")
                    .param("ecli", DEROGATOR)
                    .param("id", newVersionId)
                    .update();

            // Asked as of 2018, between the two dates: the provision was still good law then,
            // so nothing may be reported. Dating the derogation from valid_from instead would
            // report it two years early — a false red, the one error D8 rules out outright.
            assertThat(provisionAt(LocalDate.parse("2018-05-05")).derogatedBy()).isNull();

            // Asked after the annulment, the derogation is reported and drives the red.
            assertThat(provisionAt(LocalDate.parse("2021-01-01")).derogatedBy())
                    .isEqualTo(DEROGATOR);
        }

        private ProvisionEvidence onlyProvision() {
            return provisionAt(AS_OF);
        }

        private ProvisionEvidence provisionAt(LocalDate asOf) {
            List<ProvisionEvidence> found =
                    provisions.reliedOn(List.of(RELIES), asOf).getOrDefault(RELIES, List.of());
            assertThat(found).hasSize(1);
            return found.getFirst();
        }
    }

    @Nested
    @DisplayName("source links")
    class SourceUrls {

        @Test
        @DisplayName("an ECLI in the corpus maps to the page it was crawled from")
        void mapsKnownEclis() {
            insertDecision(CITED, "NSS", "panel", "2015-03-12");
            insertDecision(EXTENDED, "NSS", "extended", "2018-06-01");

            assertThat(decisions.sourceUrls(List.of(CITED, EXTENDED)))
                    .containsOnlyKeys(CITED, EXTENDED)
                    .containsEntry(CITED, "https://example.invalid/" + CITED);
        }

        @Test
        @DisplayName("an ECLI outside the corpus is absent, never mapped to null")
        void omitsUnknownEclis() {
            insertDecision(CITED, "NSS", "panel", "2015-03-12");

            // The evidence panel checks for a key. A key present with a null or empty value
            // would render as an anchor going nowhere, which reads as a working link to a
            // document we do not hold.
            assertThat(decisions.sourceUrls(List.of(CITED, PANEL))).containsOnlyKeys(CITED);
        }

        @Test
        @DisplayName("no ECLIs means no query")
        void emptyIsEmpty() {
            assertThat(decisions.sourceUrls(List.of())).isEmpty();
        }
    }

    // --- fixtures ------------------------------------------------------------

    private Status evaluate(String ecli) {
        return EvaluationContext.lazy(AS_OF, "treatment-classify.v1", corpus::coverage)
                .callWith(() -> statuses.evaluate(ecli).status());
    }

    private static EvidenceRow row(List<EvidenceRow> rows, String citingEcli) {
        return rows.stream()
                .filter(candidate -> candidate.citingEcli().equals(citingEcli))
                .findFirst()
                .orElseThrow(() -> new AssertionError("no row cited by " + citingEcli));
    }

    private void insertDecision(String ecli, String court, String panelType, String decidedOn) {
        jdbc.sql("""
                insert into decision (ecli, court_code, panel_type, decided_on, source_url,
                                      fetched_at)
                values (:ecli, :court, :panel, :decidedOn, :url, now())
                """)
                .param("ecli", ecli)
                .param("court", court)
                .param("panel", panelType)
                .param("decidedOn", LocalDate.parse(decidedOn))
                .param("url", "https://example.invalid/" + ecli)
                .update();
    }

    private void insertCorpusMeta(String court, int count, LocalDate through) {
        jdbc.sql("""
                insert into corpus_meta (court_code, decision_count, covered_through,
                                         refreshed_at)
                values (:court, :count, :through, now())
                """)
                .param("court", court)
                .param("count", count)
                .param("through", through)
                .update();
    }

    private void departed(String citing, String cited, String span, int paragraphIdx) {
        treat(citing, cited, paragraphIdx, "DEPARTED", span);
    }

    private void followed(String citing, String cited, String span, int paragraphIdx) {
        treat(citing, cited, paragraphIdx, "FOLLOWED", span);
    }

    private void quashed(String citing, String cited, String span, int paragraphIdx) {
        treat(citing, cited, paragraphIdx, "QUASHED", span);
    }

    private void treat(
            String citing, String cited, int paragraphIdx, String label, String span) {
        long citationId = jdbc.sql("""
                insert into citation (citing_ecli, cited_ecli, paragraph_idx, raw_text,
                                      extractor)
                values (:citing, :cited, :paragraphIdx, :rawText, 'rules')
                returning id
                """)
                .param("citing", citing)
                .param("cited", cited)
                .param("paragraphIdx", paragraphIdx)
                .param("rawText", "TEST- odkaz na " + cited)
                .query(Long.class)
                .single();
        jdbc.sql("""
                insert into treatment (citation_id, label, confidence, evidence_span, route,
                                       prompt_version)
                values (:citationId, :label, 0.95, :span, 'structural', 'treatment-classify.v1')
                """)
                .param("citationId", citationId)
                .param("label", label)
                .param("span", span)
                .update();
    }

    private void citesProvision(String citing, long provisionId, int paragraphIdx) {
        jdbc.sql("""
                insert into citation (citing_ecli, cited_provision, paragraph_idx, raw_text,
                                      extractor)
                values (:citing, :provisionId, :paragraphIdx, '§ 2000 odst. 1', 'rules')
                """)
                .param("citing", citing)
                .param("provisionId", provisionId)
                .param("paragraphIdx", paragraphIdx)
                .update();
    }

    private long insertProvision(String actNo, String section, String subsec) {
        return jdbc.sql("""
                insert into provision (act_no, section, subsec)
                values (:actNo, :section, :subsec)
                returning id
                """)
                .param("actNo", actNo)
                .param("section", section)
                .param("subsec", subsec)
                .query(Long.class)
                .single();
    }

    private long insertVersion(
            long provisionId, String body, String validFrom, @Nullable String validTo) {
        return jdbc.sql("""
                insert into provision_version (provision_id, body, valid_from, valid_to)
                values (:provisionId, :body, :validFrom, :validTo)
                returning id
                """)
                .param("provisionId", provisionId)
                .param("body", body)
                .param("validFrom", LocalDate.parse(validFrom))
                .param("validTo", validTo == null ? null : LocalDate.parse(validTo))
                .query(Long.class)
                .single();
    }

    private void insertMateriality(long fromVersionId, long toVersionId, boolean material) {
        jdbc.sql("""
                insert into provision_materiality (from_version_id, to_version_id,
                                                   prompt_version, material, confidence,
                                                   evidence_span)
                values (:fromVersionId, :toVersionId, 'provision-materiality.v1', :material,
                        0.90, :span)
                """)
                .param("fromVersionId", fromVersionId)
                .param("toVersionId", toVersionId)
                .param("material", material)
                .param("span", "TEST- Změna se dotýká podmínky, o kterou se odůvodnění opíralo.")
                .update();
    }
}

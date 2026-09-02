package tech.judikatguard.status;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvFileSource;

/**
 * The truth table. PLAN.md section 9 says this is the most tested class in the repo, and the
 * CSV is the specification: every line of the evaluation table plus the precedence,
 * threshold and asOf cases.
 *
 * <p>Identifiers here are all {@code TEST-} prefixed so they cannot resolve against crawled
 * data. The act and section numbers are the ones PLAN.md uses as its worked example.
 */
class StatusEngineTest {

    private static final String ECLI = "TEST-SUBJECT";
    private static final LocalDate CORPUS_THROUGH = LocalDate.parse("2026-08-15");
    private static final int CORPUS_SIZE = 3142;

    // PLAN.md section 6 and 7 use exactly this provision as the worked example.
    private static final String ACT_NO = "89/2012";
    private static final String SECTION = "2000";
    private static final String SUBSEC = "1";

    private static final String EMPTY = "-";

    // ---------------------------------------------------------------- truth table

    @ParameterizedTest(name = "[{index}] {0}: {7}")
    @CsvFileSource(resources = "/status/truth-table.csv", numLinesToSkip = 1)
    @DisplayName("truth table")
    void truthTable(
            String name,
            String asOf,
            String treatments,
            String provisions,
            String inherited,
            String expectedLight,
            String expectedReasons,
            String comment) {

        Evaluation evaluation = new Evaluation(
                ECLI,
                LocalDate.parse(asOf),
                CORPUS_SIZE,
                CORPUS_THROUGH,
                parseTreatments(treatments),
                parseProvisions(provisions),
                parseInherited(inherited),
                Thresholds.defaults());

        Status status = StatusEngine.evaluate(evaluation);

        assertThat(status.light())
                .as("light for case '%s' (%s)", name, comment)
                .isEqualTo(Light.valueOf(expectedLight));
        assertThat(reasonNames(status))
                .as("reasons for case '%s' (%s)", name, comment)
                .containsExactlyElementsOf(splitList(expectedReasons));
        assertThat(status.ecli()).isEqualTo(ECLI);
        assertThat(status.asOf()).isEqualTo(LocalDate.parse(asOf));
        assertThat(status.corpusSize()).isEqualTo(CORPUS_SIZE);
        assertThat(status.corpusThrough()).isEqualTo(CORPUS_THROUGH);
    }

    @Test
    @DisplayName("the truth table covers every row of the evaluation table")
    void truthTableCoversEveryReasonVariant() {
        // Guards against a Reason variant being added without a case being written for it.
        Status status = StatusEngine.evaluate(everything(LocalDate.parse("2026-09-01")));
        assertThat(reasonNames(status))
                .containsExactlyInAnyOrderElementsOf(
                        List.of(
                                "Quashed",
                                "Superseded",
                                "ProvisionDerogated",
                                "Conflict",
                                "Narrowed",
                                "ProvisionReworded",
                                "NeedsReview",
                                "InheritedWeakness"));
        assertThat(Reason.class.getPermittedSubclasses()).hasSize(8);
    }

    // ---------------------------------------------------------------- reason ordering

    @Test
    @DisplayName("reasons come back in evaluation-table order regardless of input order")
    void reasonsFollowTableOrder() {
        // Deliberately supplied in reverse order of the table.
        List<EvidenceRow> shuffled = List.of(
                row("TEST-U1", TreatmentLabel.UNCLASSIFIED, "0.00", "2022-05-14", false),
                row("TEST-N1", TreatmentLabel.NARROWED, "0.80", "2018-04-13", false),
                row("TEST-D2", TreatmentLabel.DEPARTED, "0.90", "2021-03-12", false),
                row("TEST-D1", TreatmentLabel.DEPARTED, "0.95", "2020-02-11", true),
                row("TEST-Q1", TreatmentLabel.QUASHED, "0.99", "2019-01-10", false));

        Status status = StatusEngine.evaluate(
                new Evaluation(
                        ECLI,
                        LocalDate.parse("2026-09-01"),
                        CORPUS_SIZE,
                        CORPUS_THROUGH,
                        shuffled,
                        List.of(reworded(7L, "2017-04-01", true), derogated(8L, "TEST-DEROG-1")),
                        List.of(new InheritedEvidence("TEST-VIA-1", Light.RED)),
                        Thresholds.defaults()));

        assertThat(status.light()).isEqualTo(Light.RED);
        assertThat(status.reasons())
                .containsExactly(
                        new Reason.Quashed("TEST-Q1", span("TEST-Q1")),
                        new Reason.Superseded("TEST-D1", "extended", span("TEST-D1")),
                        new Reason.ProvisionDerogated(8L, "TEST-DEROG-1"),
                        new Reason.Conflict("TEST-D2", span("TEST-D2")),
                        new Reason.Narrowed("TEST-N1", span("TEST-N1")),
                        new Reason.ProvisionReworded(7L, LocalDate.parse("2017-04-01"), true),
                        new Reason.NeedsReview("TEST-U1", citationId("TEST-U1")),
                        new Reason.InheritedWeakness("TEST-VIA-1"));
    }

    @Test
    @DisplayName("within one table row reasons are ordered by citing date then citing ECLI")
    void reasonsWithinARowAreStablyOrdered() {
        List<EvidenceRow> rows = List.of(
                row("TEST-N3", TreatmentLabel.NARROWED, "0.80", "2020-01-01", false),
                row("TEST-N1", TreatmentLabel.NARROWED, "0.80", "2020-01-01", false),
                row("TEST-N2", TreatmentLabel.NARROWED, "0.80", "2018-04-13", false));

        Status status = StatusEngine.evaluate(evaluationOf(rows, List.of(), List.of()));

        assertThat(status.reasons())
                .containsExactly(
                        new Reason.Narrowed("TEST-N2", span("TEST-N2")),
                        new Reason.Narrowed("TEST-N1", span("TEST-N1")),
                        new Reason.Narrowed("TEST-N3", span("TEST-N3")));
    }

    @Test
    @DisplayName("provision reasons are ordered by provision id and inherited ones by ECLI")
    void provisionAndInheritedReasonsAreStablyOrdered() {
        Status status = StatusEngine.evaluate(
                new Evaluation(
                        ECLI,
                        LocalDate.parse("2026-09-01"),
                        CORPUS_SIZE,
                        CORPUS_THROUGH,
                        List.of(),
                        List.of(reworded(8L, "2015-02-03", true), reworded(7L, "2017-04-01", true)),
                        List.of(
                                new InheritedEvidence("TEST-VIA-2", Light.RED),
                                new InheritedEvidence("TEST-VIA-1", Light.RED)),
                        Thresholds.defaults()));

        assertThat(status.reasons())
                .containsExactly(
                        new Reason.ProvisionReworded(7L, LocalDate.parse("2017-04-01"), true),
                        new Reason.ProvisionReworded(8L, LocalDate.parse("2015-02-03"), true),
                        new Reason.InheritedWeakness("TEST-VIA-1"),
                        new Reason.InheritedWeakness("TEST-VIA-2"));
    }

    @Test
    @DisplayName("Superseded carries the panel that held the departure authority")
    void supersededCarriesThePanel() {
        EvidenceRow extended = new EvidenceRow(
                42L,
                "TEST-D1",
                "NSS",
                "extended",
                LocalDate.parse("2020-02-11"),
                TreatmentLabel.DEPARTED,
                new BigDecimal("0.95"),
                "překonáno rozšířeným senátem",
                34,
                true);

        Status status = StatusEngine.evaluate(evaluationOf(List.of(extended), List.of(), List.of()));

        assertThat(status.reasons())
                .containsExactly(
                        new Reason.Superseded("TEST-D1", "extended", "překonáno rozšířeným senátem"));
    }

    // ---------------------------------------------------------------- green and empty input

    @Test
    @DisplayName("GREEN carries an empty reason list")
    void greenHasNoReasons() {
        Status status = StatusEngine.evaluate(
                evaluationOf(
                        List.of(
                                row("TEST-F1", TreatmentLabel.FOLLOWED, "0.99", "2019-01-10", false),
                                row("TEST-M1", TreatmentLabel.MENTIONED, "0.95", "2019-02-10", false),
                                row("TEST-X1", TreatmentLabel.DISTINGUISHED, "0.91", "2019-03-10", false),
                                row("TEST-K1", TreatmentLabel.CRITICIZED, "0.93", "2019-04-10", false)),
                        List.of(),
                        List.of()));

        assertThat(status.light()).isEqualTo(Light.GREEN);
        assertThat(status.reasons()).isEmpty();
    }

    @Test
    @DisplayName("all-empty evidence lists evaluate to GREEN and keep the scope fields")
    void emptyEvidenceIsGreen() {
        Status status = StatusEngine.evaluate(evaluationOf(List.of(), List.of(), List.of()));

        assertThat(status.light()).isEqualTo(Light.GREEN);
        assertThat(status.reasons()).isEmpty();
        assertThat(status.ecli()).isEqualTo(ECLI);
        assertThat(status.asOf()).isEqualTo(LocalDate.parse("2026-09-01"));
        assertThat(status.corpusSize()).isEqualTo(CORPUS_SIZE);
        assertThat(status.corpusThrough()).isEqualTo(CORPUS_THROUGH);
    }

    @Test
    @DisplayName("the returned reason list is unmodifiable")
    void reasonListIsUnmodifiable() {
        Status status = StatusEngine.evaluate(
                evaluationOf(
                        List.of(row("TEST-Q1", TreatmentLabel.QUASHED, "0.99", "2019-01-10", false)),
                        List.of(),
                        List.of()));

        assertThatThrownBy(() -> status.reasons().add(new Reason.InheritedWeakness("TEST-VIA-1")))
                .isInstanceOf(UnsupportedOperationException.class);
    }

    // ---------------------------------------------------------------- purity

    @Test
    @DisplayName("evaluate is pure: same input twice gives an equal result and no side effects")
    void evaluateIsPure() {
        Evaluation evaluation = everything(LocalDate.parse("2026-09-01"));

        Status first = StatusEngine.evaluate(evaluation);
        Status second = StatusEngine.evaluate(evaluation);

        assertThat(first).isEqualTo(second);
        assertThat(evaluation.treatments()).hasSize(5);
    }

    @Test
    @DisplayName("mutating the caller's lists after construction cannot change a verdict")
    void evidenceListsAreDefensivelyCopied() {
        List<EvidenceRow> mutable = new ArrayList<>();
        mutable.add(row("TEST-F1", TreatmentLabel.FOLLOWED, "0.99", "2019-01-10", false));
        Evaluation evaluation = evaluationOf(mutable, List.of(), List.of());

        mutable.add(row("TEST-Q1", TreatmentLabel.QUASHED, "0.99", "2019-01-10", false));

        assertThat(StatusEngine.evaluate(evaluation).light()).isEqualTo(Light.GREEN);
    }

    @Test
    @DisplayName("thresholds are honoured as supplied rather than read from anywhere")
    void thresholdsComeFromTheEvaluation() {
        List<EvidenceRow> rows =
                List.of(row("TEST-Q1", TreatmentLabel.QUASHED, "0.70", "2019-01-10", false));
        Thresholds lenient = new Thresholds(new BigDecimal("0.65"), new BigDecimal("0.40"));

        assertThat(StatusEngine.evaluate(evaluationOf(rows, List.of(), List.of())).light())
                .isEqualTo(Light.GREEN);
        assertThat(
                        StatusEngine.evaluate(
                                        new Evaluation(
                                                ECLI,
                                                LocalDate.parse("2026-09-01"),
                                                CORPUS_SIZE,
                                                CORPUS_THROUGH,
                                                rows,
                                                List.of(),
                                                List.of(),
                                                lenient))
                                .light())
                .isEqualTo(Light.RED);
    }

    @Test
    @DisplayName("default thresholds are the D8 asymmetric pair")
    void defaultThresholds() {
        assertThat(Thresholds.defaults().redMinConfidence()).isEqualByComparingTo("0.85");
        assertThat(Thresholds.defaults().amberMinConfidence()).isEqualByComparingTo("0.60");
    }

    // ---------------------------------------------------------------- fixtures

    /** One of every table row firing at once, used by the ordering and purity tests. */
    private static Evaluation everything(LocalDate asOf) {
        return new Evaluation(
                ECLI,
                asOf,
                CORPUS_SIZE,
                CORPUS_THROUGH,
                List.of(
                        row("TEST-Q1", TreatmentLabel.QUASHED, "0.99", "2019-01-10", false),
                        row("TEST-D1", TreatmentLabel.DEPARTED, "0.95", "2020-02-11", true),
                        row("TEST-D2", TreatmentLabel.DEPARTED, "0.90", "2021-03-12", false),
                        row("TEST-N1", TreatmentLabel.NARROWED, "0.80", "2018-04-13", false),
                        row("TEST-U1", TreatmentLabel.UNCLASSIFIED, "0.00", "2022-05-14", false)),
                List.of(reworded(7L, "2017-04-01", true), derogated(8L, "TEST-DEROG-1")),
                List.of(new InheritedEvidence("TEST-VIA-1", Light.RED)),
                Thresholds.defaults());
    }

    private static Evaluation evaluationOf(
            List<EvidenceRow> treatments,
            List<ProvisionEvidence> provisions,
            List<InheritedEvidence> inherited) {
        return new Evaluation(
                ECLI,
                LocalDate.parse("2026-09-01"),
                CORPUS_SIZE,
                CORPUS_THROUGH,
                treatments,
                provisions,
                inherited,
                Thresholds.defaults());
    }

    private static EvidenceRow row(
            String citingEcli,
            TreatmentLabel label,
            String confidence,
            String citingDate,
            boolean mayDepart) {
        return new EvidenceRow(
                citationId(citingEcli),
                citingEcli,
                "NSS",
                mayDepart ? "extended" : "panel",
                LocalDate.parse(citingDate),
                label,
                new BigDecimal(confidence),
                span(citingEcli),
                null,
                mayDepart);
    }

    private static ProvisionEvidence reworded(long id, String changedOn, boolean material) {
        return new ProvisionEvidence(
                id, ACT_NO, SECTION, SUBSEC, true, LocalDate.parse(changedOn), material, null);
    }

    private static ProvisionEvidence derogated(long id, String byEcli) {
        return new ProvisionEvidence(
                id, ACT_NO, SECTION, SUBSEC, false, LocalDate.parse("2018-06-01"), false, byEcli);
    }

    /** Deterministic stand-in for {@code citation.id}; the value only has to be stable. */
    private static long citationId(String citingEcli) {
        return citingEcli.charAt(citingEcli.length() - 1) - '0';
    }

    private static String span(String citingEcli) {
        return "TEST-SPAN " + citingEcli;
    }

    private static List<String> reasonNames(Status status) {
        return status.reasons().stream().map(r -> r.getClass().getSimpleName()).toList();
    }

    // ---------------------------------------------------------------- CSV parsing

    private static List<String> splitList(String cell) {
        if (EMPTY.equals(cell)) {
            return List.of();
        }
        return java.util.Arrays.stream(cell.split(";")).map(String::trim).toList();
    }

    /** {@code LABEL confidence citingDate citingMayDepart citingEcli} */
    private static List<EvidenceRow> parseTreatments(String cell) {
        return splitList(cell).stream()
                .map(item -> item.split("\\s+"))
                .map(f -> row(f[4], TreatmentLabel.valueOf(f[0]), f[1], f[2], Boolean.parseBoolean(f[3])))
                .toList();
    }

    /** {@code provisionId changedOn reworded material derogatedBy} */
    private static List<ProvisionEvidence> parseProvisions(String cell) {
        return splitList(cell).stream()
                .map(item -> item.split("\\s+"))
                .map(
                        f ->
                                new ProvisionEvidence(
                                        Long.parseLong(f[0]),
                                        ACT_NO,
                                        SECTION,
                                        SUBSEC,
                                        Boolean.parseBoolean(f[2]),
                                        EMPTY.equals(f[1]) ? null : LocalDate.parse(f[1]),
                                        Boolean.parseBoolean(f[3]),
                                        EMPTY.equals(f[4]) ? null : f[4]))
                .toList();
    }

    /** {@code light viaEcli} */
    private static List<InheritedEvidence> parseInherited(String cell) {
        return splitList(cell).stream()
                .map(item -> item.split("\\s+"))
                .map(f -> new InheritedEvidence(f[1], Light.valueOf(f[0])))
                .toList();
    }
}

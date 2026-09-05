package tech.judikatguard.document;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyCollection;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import java.time.LocalDate;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import tech.judikatguard.decision.AliasRepository;
import tech.judikatguard.decision.Corpus;
import tech.judikatguard.decision.CorpusCoverage;
import tech.judikatguard.decision.DecisionRepository;
import tech.judikatguard.decision.ProvisionRepository;
import tech.judikatguard.decision.ProvisionRepository.ProvisionKey;
import tech.judikatguard.document.StatusService.Evaluated;
import tech.judikatguard.extract.CitationExtractor;
import tech.judikatguard.extract.Reference;
import tech.judikatguard.status.Light;
import tech.judikatguard.status.Reason;
import tech.judikatguard.status.Status;

/**
 * Resolution and report assembly, without a database.
 *
 * <p>Every citation string in this class appears literally in PLAN.md or in
 * {@code extract/patterns.toml} (CLAUDE.md hard rule 1). That constraint is also why the
 * "does not resolve" case is carried by the provision rather than by a second case number:
 * inventing a č. j. that fails to resolve would be inventing a č. j.
 */
@ExtendWith(MockitoExtension.class)
class DocumentReadPathTest {

    /** PLAN.md section 11, the example citation. */
    private static final String REF_NO_TEXT = "č. j. 6 Ads 45/2014-32";

    private static final String PROVISION_TEXT = "§ 2000 odst. 1 zákona č. 89/2012 Sb.";

    private static final String DOCUMENT = """
            Krajský soud odkázal na rozsudek NSS ze dne 12. 3. 2015, %s.

            Dále vyšel z %s
            """.formatted(REF_NO_TEXT, PROVISION_TEXT);

    private static final String ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32";

    private static final LocalDate AS_OF = LocalDate.parse("2026-09-01");

    private static final LocalDate THROUGH = LocalDate.parse("2026-08-15");

    private final CitationExtractor extractor = CitationExtractor.withSharedPatterns();

    @Mock AliasRepository aliases;
    @Mock DecisionRepository decisions;
    @Mock ProvisionRepository provisions;
    @Mock StatusService statuses;

    @Nested
    @DisplayName("alias normalisation, mirroring jg.extract.resolver")
    class Aliases {

        @Test
        @DisplayName("a č. j. is tried with its sheet number first, then without")
        void refNoTriesBothForms() {
            Reference reference = only(REF_NO_TEXT);

            assertThat(ReferenceResolver.aliasCandidates(reference))
                    .containsExactly("6 ads 45/2014-32", "6 ads 45/2014");
        }

        @Test
        @DisplayName("an ECLI resolves on its own whole text, case-folded")
        void ecliUsesItsOwnText() {
            assertThat(ReferenceResolver.aliasCandidates(only(ECLI)))
                    .containsExactly(ECLI.toLowerCase(java.util.Locale.ROOT));
        }

        @Test
        @DisplayName("a Constitutional Court case number keeps its Roman panel numeral")
        void constitutionalCourtCaseNumber() {
            // The spelling documented in extract/patterns.toml.
            assertThat(ReferenceResolver.aliasCandidates(only("sp. zn. II. ÚS 2379/08")))
                    .containsExactly("ii. ús 2379/08");
        }

        @Test
        @DisplayName("an R-číslo is rebuilt into its canonical spacing")
        void journalNumber() {
            assertThat(ReferenceResolver.aliasCandidates(only("R 12/2015")))
                    .contains("r 12/2015");
        }

        @Test
        @DisplayName("whitespace collapses, including the non-breaking space of scraped text")
        void normalisationCollapsesWhitespace() {
            // The NBSP is deliberate. Java's \\s is ASCII-only and Python's is not, so a
            // literal mirror of normalize_alias would make the two runtimes compute
            // different aliases for this very common citation form.
            assertThat(ReferenceResolver.normalizeAlias("  6 Ads\u00A0 45/2014 \n"))
                    .isEqualTo("6 ads 45/2014");
        }
    }

    @Nested
    @DisplayName("provision anaphora")
    class Anaphora {

        @Test
        @DisplayName("a section with no act takes the most recent explicit act before it")
        void backwardScan() {
            List<Reference> references = extractor.extract("""
                    Podle %s je rozhodná dobrá víra.

                    Totéž platí pro § 2001.
                    """.formatted(PROVISION_TEXT));

            Map<Reference, String> acts = ReferenceResolver.assignProvisionActs(references);

            assertThat(acts.values()).containsOnly("89/2012");
            assertThat(acts).hasSize(2);
        }

        @Test
        @DisplayName("a section with no act anywhere in the document stays unattached")
        void noActAtAll() {
            List<Reference> references = extractor.extract("Podle § 2001 odst. 2 platí totéž.");

            assertThat(ReferenceResolver.assignProvisionActs(references)).isEmpty();
        }
    }

    @Nested
    @DisplayName("POST /api/documents/check assembly")
    class Report {

        @Test
        @DisplayName("resolved sources are reported and unresolved ones are not dropped")
        void resolvedAndUnresolved() {
            given(aliases.resolve(anyCollection()))
                    .willReturn(Map.of("6 ads 45/2014-32", ECLI));
            // The provision is a real citation this corpus simply does not hold yet.
            given(provisions.resolve(anyCollection())).willReturn(Map.of());
            given(provisions.directlyCited(anyCollection(), any())).willReturn(Map.of());
            given(statuses.evaluateAll(anyCollection())).willReturn(Map.of(ECLI, green(ECLI)));

            DocumentReport report = check(DOCUMENT);

            assertThat(report.asOf()).isEqualTo(AS_OF);
            assertThat(report.corpus()).containsKey("NSS");
            assertThat(report.sources()).hasSize(1);
            assertThat(report.sources().getFirst().ecli()).isEqualTo(ECLI);
            assertThat(report.sources().getFirst().rawText())
                    .as("the raw text is the document's own, never rebuilt from the ECLI")
                    .isEqualTo(REF_NO_TEXT);
            assertThat(report.sources().getFirst().light()).isEqualTo(Light.GREEN);
            assertThat(report.unresolved())
                    .containsExactly(new UnresolvedReference(
                            PROVISION_TEXT, ReferenceResolver.NOT_IN_CORPUS));
        }

        @Test
        @DisplayName("a resolved provision is a source with a null ecli")
        void provisionSource() {
            ProvisionKey key = new ProvisionKey("89/2012", "2000", "1");
            given(aliases.resolve(anyCollection())).willReturn(Map.of());
            given(provisions.resolve(anyCollection())).willReturn(Map.of(key, 7L));
            given(provisions.directlyCited(anyCollection(), any())).willReturn(Map.of());
            given(statuses.evaluateAll(anyCollection())).willReturn(Map.of());

            DocumentReport report = check("Podle " + PROVISION_TEXT);

            assertThat(report.sources()).hasSize(1);
            assertThat(report.sources().getFirst().ecli()).isNull();
            assertThat(report.sources().getFirst().rawText()).isEqualTo(PROVISION_TEXT);
            assertThat(report.unresolved()).isEmpty();
        }

        @Test
        @DisplayName("a source cited twice is one row")
        void duplicatesCollapse() {
            given(aliases.resolve(anyCollection()))
                    .willReturn(Map.of("6 ads 45/2014-32", ECLI));
            given(provisions.resolve(anyCollection())).willReturn(Map.of());
            given(provisions.directlyCited(anyCollection(), any())).willReturn(Map.of());
            given(statuses.evaluateAll(anyCollection())).willReturn(Map.of(ECLI, green(ECLI)));

            DocumentReport report = check(REF_NO_TEXT + "\n\nznovu " + REF_NO_TEXT);

            assertThat(report.sources()).hasSize(1);
        }

        @Test
        @DisplayName("the link table is asked for the citing decisions, not just the sources")
        void linksCoverCitingDecisions() {
            String citing = "TEST-ECLI-NSS-NARROWED";
            String url = "https://example.invalid/" + citing;
            given(aliases.resolve(anyCollection()))
                    .willReturn(Map.of("6 ads 45/2014-32", ECLI));
            given(provisions.resolve(anyCollection())).willReturn(Map.of());
            given(provisions.directlyCited(anyCollection(), any())).willReturn(Map.of());
            given(statuses.evaluateAll(anyCollection()))
                    .willReturn(Map.of(ECLI, narrowedBy(ECLI, citing)));
            given(decisions.sourceUrls(anyCollection())).willReturn(Map.of(citing, url));

            DocumentReport report = check(REF_NO_TEXT);

            // The decision the reader has never seen is the one they most need to open, so
            // the citing ECLI has to reach the repository even though the document never
            // named it.
            ArgumentCaptor<Collection<String>> asked = ArgumentCaptor.captor();
            verify(decisions).sourceUrls(asked.capture());
            assertThat(asked.getValue()).contains(ECLI, citing);
            assertThat(report.links()).containsExactly(Map.entry(citing, url));
        }

        @Test
        @DisplayName("a document with no citations reports empty arrays, not an error")
        void nothingToReport() {
            given(aliases.resolve(anyCollection())).willReturn(Map.of());
            given(provisions.resolve(anyCollection())).willReturn(Map.of());
            given(statuses.evaluateAll(anyCollection())).willReturn(Map.of());

            DocumentReport report = check("Tento text neobsahuje žádný odkaz.");

            assertThat(report.sources()).isEmpty();
            assertThat(report.unresolved()).isEmpty();
            verify(provisions, never()).reliedOn(anyCollection(), any());
        }
    }

    // --- fixtures ------------------------------------------------------------

    private DocumentReport check(String text) {
        DocumentChecker checker = new DocumentChecker(
                extractor,
                new ReferenceResolver(aliases, provisions),
                statuses,
                provisions,
                decisions);
        return EvaluationContext
                .lazy(AS_OF, "proposition-check.v1", DocumentReadPathTest::corpus)
                .callWith(() -> checker.check(text));
    }

    private static Corpus corpus() {
        return Corpus.of(Map.of("NSS", new CorpusCoverage(3142, THROUGH)));
    }

    private static Evaluated green(String ecli) {
        return new Evaluated(
                new Status(ecli, Light.GREEN, AS_OF, 3142, THROUGH, List.of()), List.of());
    }

    private static Evaluated narrowedBy(String ecli, String citing) {
        return new Evaluated(
                new Status(
                        ecli,
                        Light.AMBER,
                        AS_OF,
                        3142,
                        THROUGH,
                        List.of(new Reason.Narrowed(
                                citing, "TEST- Závěr dopadá jen na řízení zahájená po novele."))),
                List.of());
    }

    /** The single reference the rules pass finds in {@code text}. */
    private Reference only(String text) {
        List<Reference> references = extractor.extract(text);
        assertThat(references).as("expected exactly one reference in %s", text).hasSize(1);
        return references.getFirst();
    }
}

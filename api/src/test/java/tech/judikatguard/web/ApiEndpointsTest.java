package tech.judikatguard.web;

import static org.hamcrest.Matchers.containsString;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.mockingDetails;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.LocalDate;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import tech.judikatguard.decision.Corpus;
import tech.judikatguard.decision.CorpusCoverage;
import tech.judikatguard.decision.CorpusRepository;
import tech.judikatguard.decision.DecisionRepository;
import tech.judikatguard.decision.DecisionSummary;
import tech.judikatguard.document.DocumentChecker;
import tech.judikatguard.document.DocumentReport;
import tech.judikatguard.document.PropositionService;
import tech.judikatguard.document.ReasonView;
import tech.judikatguard.document.SourceReport;
import tech.judikatguard.document.StatusService;
import tech.judikatguard.document.UnresolvedReference;
import tech.judikatguard.llm.LlmClient;
import tech.judikatguard.llm.PropositionVerdict;
import tech.judikatguard.status.Light;
import tech.judikatguard.status.Reason;
import tech.judikatguard.status.Status;

/**
 * The HTTP contract of PLAN.md section 11, as a Spring Boot 4 MVC slice.
 *
 * <p>The slice annotation is {@code org.springframework.boot.webmvc.test.autoconfigure} —
 * Boot 4 split the test slices into one module per web stack, and the Boot 3 package no
 * longer exists.
 *
 * <p>Everything below the controllers is mocked on purpose: what is under test here is the
 * wire format, the status codes, and the Czech wording. The graph queries have their own
 * container-backed test and the verdict policy has its truth table.
 *
 * <p>Identifiers are either taken literally from PLAN.md section 11 or prefixed
 * {@code TEST-} so they cannot resolve (CLAUDE.md hard rule 1).
 */
@WebMvcTest
class ApiEndpointsTest {

    /** The example row of PLAN.md section 11, verbatim. */
    private static final String ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32";

    private static final String RAW_TEXT = "rozsudek NSS ze dne 12. 3. 2015, č. j. 6 Ads 45/2014-32";

    private static final String NARROWING_ECLI = "TEST-ECLI-NSS-NARROWED";

    /** A {@code .invalid} host, so a fixture link can never reach a real court. */
    private static final String NARROWING_URL = "https://example.invalid/" + NARROWING_ECLI;

    private static final String SPAN =
            "TEST- Závěr se uplatní pouze tam, kde účastník unesl břemeno tvrzení.";

    private static final LocalDate AS_OF = LocalDate.parse("2026-09-01");

    private static final LocalDate THROUGH = LocalDate.parse("2026-08-15");

    @Autowired MockMvc mvc;

    @MockitoBean DecisionRepository decisions;
    @MockitoBean StatusService statuses;
    @MockitoBean DocumentChecker checker;
    @MockitoBean PropositionService propositions;
    @MockitoBean CorpusRepository corpus;

    @Test
    @DisplayName("GET /api/health answers 200 without touching the database")
    void health() throws Exception {
        mvc.perform(get("/api/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("ok"));

        assertThat(mockingDetails(corpus).getInvocations())
                .as("health must not query corpus_meta")
                .isEmpty();
    }

    @Nested
    @DisplayName("GET /api/corpus")
    class CorpusEndpoint {

        @Test
        @DisplayName("reports size and per-court coverage")
        void reportsCoverage() throws Exception {
            given(corpus.coverage()).willReturn(nssCorpus());

            mvc.perform(get("/api/corpus"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.size").value(3142))
                    .andExpect(jsonPath("$.through").value("2026-08-15"))
                    .andExpect(jsonPath("$.courts.NSS.count").value(3142))
                    .andExpect(jsonPath("$.courts.NSS.through").value("2026-08-15"));
        }
    }

    @Nested
    @DisplayName("GET /api/decisions/{ecli}")
    class DecisionEndpoint {

        @Test
        @DisplayName("returns metadata and the ratio summary")
        void returnsMetadata() throws Exception {
            given(decisions.find(ECLI)).willReturn(Optional.of(summary()));

            mvc.perform(get("/api/decisions/{ecli}", ECLI))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.ecli").value(ECLI))
                    .andExpect(jsonPath("$.court").value("NSS"))
                    .andExpect(jsonPath("$.panelType").value("panel"))
                    .andExpect(jsonPath("$.decidedOn").value("2015-03-12"))
                    .andExpect(jsonPath("$.journalNo").doesNotExist());
        }

        @Test
        @DisplayName("404 for an ECLI the corpus has never seen, and says so in Czech")
        void unknownEcliIsNotFound() throws Exception {
            given(decisions.find(anyString())).willReturn(Optional.empty());

            mvc.perform(get("/api/decisions/{ecli}", "TEST-ECLI-NSS-ABSENT"))
                    .andExpect(status().isNotFound())
                    .andExpect(content().contentTypeCompatibleWith("application/problem+json"))
                    .andExpect(jsonPath("$.title").value(Wording.NOT_FOUND_TITLE))
                    .andExpect(jsonPath("$.detail").value(
                            containsString("není v korpusu")));
        }
    }

    @Nested
    @DisplayName("GET /api/decisions/{ecli}/status")
    class StatusEndpoint {

        @Test
        @DisplayName("GREEN carries the 'no adverse treatment found' phrasing, never 'platný'")
        void greenIsScopedAndNeverClaimsValidity() throws Exception {
            given(decisions.find(ECLI)).willReturn(Optional.of(summary()));
            given(statuses.evaluate(ECLI)).willReturn(new StatusService.Evaluated(
                    new Status(ECLI, Light.GREEN, AS_OF, 3142, THROUGH, List.of()), List.of()));

            String body = mvc.perform(get("/api/decisions/{ecli}/status", ECLI)
                            .param("asOf", "2026-09-01"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.light").value("GREEN"))
                    .andExpect(jsonPath("$.asOf").value("2026-09-01"))
                    .andExpect(jsonPath("$.corpusSize").value(3142))
                    .andExpect(jsonPath("$.corpusThrough").value("2026-08-15"))
                    .andExpect(jsonPath("$.reasons").isEmpty())
                    .andReturn()
                    .getResponse()
                    .getContentAsString();

            assertThat(body).contains(Wording.NO_ADVERSE_TREATMENT);
            assertThat(body).doesNotContain("platn");
        }

        @Test
        @DisplayName("a binding departure is 'překonáno' and carries its span and paragraph")
        void redUsesCzechLegalVocabulary() throws Exception {
            given(decisions.find(ECLI)).willReturn(Optional.of(summary()));
            given(statuses.evaluate(ECLI)).willReturn(new StatusService.Evaluated(
                    new Status(
                            ECLI,
                            Light.RED,
                            AS_OF,
                            3142,
                            THROUGH,
                            List.of(new Reason.Superseded(NARROWING_ECLI, "extended", SPAN))),
                    List.of()));

            String body = mvc.perform(get("/api/decisions/{ecli}/status", ECLI))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.light").value("RED"))
                    .andExpect(jsonPath("$.reasons[0].kind").value(ReasonView.SUPERSEDED))
                    .andExpect(jsonPath("$.reasons[0].byEcli").value(NARROWING_ECLI))
                    .andExpect(jsonPath("$.reasons[0].panel").value("extended"))
                    .andExpect(jsonPath("$.reasons[0].span").value(SPAN))
                    .andReturn()
                    .getResponse()
                    .getContentAsString();

            assertThat(body).contains("překonáno");
            assertNoCommonLawTerms(body);
        }

        @Test
        @DisplayName("404 rather than a green light when the ECLI is not in the corpus")
        void unknownEcliIsNotEvaluated() throws Exception {
            given(decisions.find(anyString())).willReturn(Optional.empty());

            mvc.perform(get("/api/decisions/{ecli}/status", "TEST-ECLI-NSS-ABSENT"))
                    .andExpect(status().isNotFound());

            verify(statuses, never()).evaluate(anyString());
        }

        @Test
        @DisplayName("an unparseable asOf is rejected in Czech before anything is evaluated")
        void badAsOfIsRejected() throws Exception {
            mvc.perform(get("/api/decisions/{ecli}/status", ECLI).param("asOf", "1. 9. 2026"))
                    .andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.title").value(Wording.BAD_REQUEST_TITLE))
                    .andExpect(jsonPath("$.detail").value(
                            containsString("RRRR-MM-DD")));
        }
    }

    @Nested
    @DisplayName("POST /api/documents/check")
    class DocumentCheck {

        @Test
        @DisplayName("matches the DocumentReport JSON of PLAN.md section 11")
        void matchesTheSpec() throws Exception {
            given(checker.check(anyString())).willReturn(report());

            mvc.perform(post("/api/documents/check")
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"text\": \"TEST- text s odkazem.\"}"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.asOf").value("2026-09-01"))
                    .andExpect(jsonPath("$.corpus.NSS.count").value(3142))
                    .andExpect(jsonPath("$.corpus.NSS.through").value("2026-08-15"))
                    .andExpect(jsonPath("$.sources[0].ecli").value(ECLI))
                    .andExpect(jsonPath("$.sources[0].rawText").value(RAW_TEXT))
                    .andExpect(jsonPath("$.sources[0].light").value("AMBER"))
                    .andExpect(jsonPath("$.sources[0].reasons[0].kind").value(ReasonView.NARROWED))
                    .andExpect(jsonPath("$.sources[0].reasons[0].byEcli").value(NARROWING_ECLI))
                    .andExpect(jsonPath("$.sources[0].reasons[0].span").value(SPAN))
                    .andExpect(jsonPath("$.sources[0].reasons[0].paragraph").value(34))
                    .andExpect(jsonPath("$.links['" + NARROWING_ECLI + "']").value(NARROWING_URL))
                    .andExpect(jsonPath("$.unresolved[0].rawText").value("citovaného rozhodnutí"))
                    .andExpect(jsonPath("$.unresolved[0].reason").value("not resolved"));
        }

        @Test
        @DisplayName("an ecli with no link is absent from 'links' rather than mapped to null")
        void linksOmitWhatIsNotInTheCorpus() throws Exception {
            // The cited source itself has no entry here. The evidence panel must render it as
            // plain text: an anchor with an empty href would look like a working link to a
            // document we do not have.
            given(checker.check(anyString())).willReturn(report());

            mvc.perform(post("/api/documents/check")
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"text\": \"TEST- text s odkazem.\"}"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.links['" + ECLI + "']").doesNotExist())
                    .andExpect(jsonPath("$.links").isMap());
        }

        @Test
        @DisplayName("a provision source has a null ecli, as the web types expect")
        void provisionSourceHasNoEcli() throws Exception {
            given(checker.check(anyString())).willReturn(new DocumentReport(
                    AS_OF,
                    Map.of("NSS", new CorpusCoverage(3142, THROUGH)),
                    List.of(new SourceReport(
                            null, "§ 2000 odst. 1 zákona č. 89/2012 Sb.", Light.GREEN, List.of())),
                    Map.of(),
                    List.of()));

            mvc.perform(post("/api/documents/check")
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"text\": \"§ 2000 odst. 1 zákona č. 89/2012 Sb.\"}"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.sources[0].ecli").value((Object) null))
                    .andExpect(jsonPath("$.sources[0].light").value("GREEN"));
        }

        @Test
        @DisplayName("'unresolved' is always present, empty array included")
        void unresolvedIsAlwaysPresent() throws Exception {
            given(checker.check(anyString())).willReturn(new DocumentReport(
                    AS_OF, Map.of(), List.of(), Map.of(), List.of()));

            mvc.perform(post("/api/documents/check")
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"text\": \"TEST- bez odkazů.\"}"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.unresolved").isArray())
                    .andExpect(jsonPath("$.unresolved").isEmpty())
                    .andExpect(jsonPath("$.sources").isArray());
        }

        @Test
        @DisplayName("an empty document is a 400 in Czech")
        void blankTextIsRejected() throws Exception {
            mvc.perform(post("/api/documents/check")
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"text\": \"   \"}"))
                    .andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.detail").value(
                            containsString("nesmí být prázdný")));
        }
    }

    @Nested
    @DisplayName("POST /api/citations/{id}/proposition-check")
    class PropositionCheck {

        @Test
        @DisplayName("returns the verdict with its verbatim span")
        void returnsVerdict() throws Exception {
            given(propositions.check(anyLong(), anyString())).willReturn(Optional.of(
                    new PropositionVerdict(
                            PropositionVerdict.OVERBROAD,
                            0.82,
                            SPAN,
                            "Rozhodnutí uvedený závěr vyslovilo jen pro věci movité.")));

            mvc.perform(post("/api/citations/{id}/proposition-check", 1)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"claim\": \"TEST- tvrzení z dokumentu.\"}"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.verdict").value("OVERBROAD"))
                    .andExpect(jsonPath("$.evidenceSpan").value(SPAN));
        }

        @Test
        @DisplayName("404 for an unknown citation or one that names a provision")
        void unknownCitation() throws Exception {
            given(propositions.check(anyLong(), anyString())).willReturn(Optional.empty());

            mvc.perform(post("/api/citations/{id}/proposition-check", 404)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"claim\": \"TEST- tvrzení z dokumentu.\"}"))
                    .andExpect(status().isNotFound());
        }

        @Test
        @DisplayName("503, not a verdict, when no model call can be made")
        void modelUnavailable() throws Exception {
            given(propositions.check(anyLong(), anyString()))
                    .willThrow(new LlmClient.Unavailable("ANTHROPIC_API_KEY is not set"));

            mvc.perform(post("/api/citations/{id}/proposition-check", 1)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"claim\": \"TEST- tvrzení z dokumentu.\"}"))
                    .andExpect(status().isServiceUnavailable())
                    .andExpect(jsonPath("$.title").value(Wording.LLM_UNAVAILABLE_TITLE));
        }
    }

    // --- fixtures ------------------------------------------------------------

    private static void assertNoCommonLawTerms(String body) {
        assertThat(body.toLowerCase(Locale.ROOT))
                .as("Czech law has no doctrine of binding precedent; PLAN.md section 16")
                .doesNotContain("overrul")
                .doesNotContain("distinguish");
    }

    private static Corpus nssCorpus() {
        return Corpus.of(Map.of("NSS", new CorpusCoverage(3142, THROUGH)));
    }

    private static DecisionSummary summary() {
        return new DecisionSummary(
                ECLI,
                "NSS",
                "panel",
                LocalDate.parse("2015-03-12"),
                "6 Ads 45/2014",
                "6 Ads 45/2014-32",
                null,
                "TEST- právní věta pro účely testu.",
                "https://example.invalid/" + ECLI);
    }

    private static DocumentReport report() {
        return new DocumentReport(
                AS_OF,
                Map.of("NSS", new CorpusCoverage(3142, THROUGH)),
                List.of(new SourceReport(
                        ECLI,
                        RAW_TEXT,
                        Light.AMBER,
                        List.of(new ReasonView(
                                ReasonView.NARROWED, NARROWING_ECLI, SPAN, 34, null, null, null,
                                null, null)))),
                Map.of(NARROWING_ECLI, NARROWING_URL),
                List.of(new UnresolvedReference("citovaného rozhodnutí", "not resolved")));
    }
}

package tech.judikatguard.extract;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import tech.judikatguard.extract.PatternSet.CompiledPattern;
import tech.judikatguard.extract.Paragraphs.ContextWindow;

/**
 * The rules pass, pattern by pattern.
 *
 * <p>Every citation string here is quoted literally from {@code PLAN.md} or from
 * {@code extract/patterns.toml} (CLAUDE.md rule 1); the only fabricated identifiers are the
 * {@code TEST-} ones in the overlap-policy tests, which are never fed to a regex and cannot
 * resolve. {@code ExtractionParityTest} is the test that pins agreement with the Python
 * implementation; this one pins the behaviour the two agree on.
 */
class CitationExtractorTest {

    private static PatternSet patterns;
    private static CitationExtractor extractor;

    @BeforeAll
    static void loadPatterns() {
        patterns = PatternSet.fromClasspathOrRepo();
        extractor = new CitationExtractor(patterns);
    }

    /** The single reference the body is expected to contain. */
    private static Reference only(String body) {
        List<Reference> found = extractor.extractParagraph(body, 1);
        assertThat(found).as("references in %s", body).hasSize(1);
        return found.getFirst();
    }

    // ------------------------------------------------------------------ pattern loading

    @Nested
    @DisplayName("extract/patterns.toml")
    class Loading {

        @Test
        @DisplayName("loads every pattern, in the order the file declares them")
        void declarationOrder() throws IOException {
            Path tomlPath = PatternSet.findInRepo();
            assertThat(tomlPath).as("extract/patterns.toml above the working directory").isNotNull();
            String toml = Files.readString(tomlPath, StandardCharsets.UTF_8);

            // Declaration order is the tie-break for identical spans in the overlap policy,
            // so it is a contract, not an accident of map iteration. Checked against the
            // file itself rather than a hard-coded list, so adding a pattern does not fail
            // this test unless the order is actually lost.
            List<Integer> positions =
                    patterns.patterns().stream()
                            .map(pattern -> toml.indexOf("[patterns." + pattern.name() + "]"))
                            .toList();
            assertThat(positions).doesNotContain(-1).isSorted();
            assertThat(patterns.patterns()).isNotEmpty();
            assertThat(patterns.version()).isPositive();
        }

        @Test
        @DisplayName("compiles with no flags, so the ASCII reading of \\s \\w \\d is kept")
        void noUnicodeCharacterClassFlag() {
            assertThat(patterns.patterns())
                    .allSatisfy(pattern -> assertThat(pattern.regex().flags()).isZero());
        }

        @Test
        @DisplayName("exposes the router marker lists and the anaphora triggers")
        void markersAndTriggers() {
            assertThat(patterns.partySubmissionMarkers()).contains("stěžovatel odkazuje");
            assertThat(patterns.departureMarkers()).contains("překonáno");
            assertThat(patterns.quashingMarkers()).contains("se ruší");
            assertThat(patterns.citationTriggers()).contains("tamtéž");
            assertThat(patterns.markers("nothing_declares_this")).isEmpty();
        }

        @Test
        @DisplayName("byName rejects an unknown pattern instead of returning null")
        void byNameIsStrict() {
            assertThatThrownBy(() -> patterns.byName("TEST-no-such-pattern"))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("TEST-no-such-pattern");
        }

        @Test
        @DisplayName("a system property overrides the search, so tests are not cwd-dependent")
        void systemPropertyOverride() {
            Path tomlPath = PatternSet.findInRepo();
            assertThat(tomlPath).isNotNull();
            String previous = System.getProperty(PatternSet.PATH_PROPERTY);
            try {
                System.setProperty(PatternSet.PATH_PROPERTY, tomlPath.toString());
                PatternSet loaded = PatternSet.fromClasspathOrRepo();
                assertThat(loaded.origin()).isEqualTo(tomlPath.toString());
                assertThat(loaded.patterns()).hasSameSizeAs(patterns.patterns());

                System.setProperty(PatternSet.PATH_PROPERTY, "TEST-no-such-patterns.toml");
                assertThatThrownBy(PatternSet::fromClasspathOrRepo)
                        .as("an override that does not exist must fail loudly, not fall back")
                        .isInstanceOf(IllegalStateException.class)
                        .hasMessageContaining(PatternSet.PATH_PROPERTY);
            } finally {
                if (previous == null) {
                    System.clearProperty(PatternSet.PATH_PROPERTY);
                } else {
                    System.setProperty(PatternSet.PATH_PROPERTY, previous);
                }
            }
        }

        @Test
        @DisplayName("a missing file is an error, never a silently empty pattern set")
        void missingFileFails() {
            Path missing = Path.of("TEST-no-such-directory", "patterns.toml");
            assertThatThrownBy(() -> PatternSet.load(missing))
                    .isInstanceOfAny(UncheckedIOException.class, IllegalStateException.class);
        }
    }

    // ------------------------------------------------------------------ one pattern at a time

    @Nested
    @DisplayName("patterns")
    class Patterns {

        @Test
        @DisplayName("ref_no keeps the sheet number as its own group")
        void refNoWithSheetNumber() {
            Reference ref = only("Rozsudek NSS ze dne 12. 3. 2015, č. j. 6 Ads 45/2014-32, na který soud odkázal.");

            assertThat(ref.kind()).isEqualTo(ReferenceKind.REF_NO);
            assertThat(ref.rawText()).isEqualTo("č. j. 6 Ads 45/2014-32");
            assertThat(ref.groups())
                    .containsExactlyInAnyOrderEntriesOf(
                            Map.of("ref_no", "6 Ads 45/2014", "sheet_no", "32"));
        }

        @Test
        @DisplayName("ref_no omits sheet_no entirely when there is no sheet number")
        void refNoWithoutSheetNumber() {
            Reference ref = only("Rozsudek č. j. 6 Ads 45/2014 byl vydán v této věci.");

            assertThat(ref.rawText()).isEqualTo("č. j. 6 Ads 45/2014");
            assertThat(ref.groups()).containsExactly(Map.entry("ref_no", "6 Ads 45/2014"));
            assertThat(ref.group("sheet_no")).isNull();
        }

        @Test
        @DisplayName("case_no_us reads the Roman panel number")
        void caseNoConstitutionalCourt() {
            Reference ref = only("Ústavní soud v nálezu sp. zn. II. ÚS 2379/08 vyložil ustanovení odlišně.");

            assertThat(ref.kind()).isEqualTo(ReferenceKind.CASE_NO);
            assertThat(ref.rawText()).isEqualTo("sp. zn. II. ÚS 2379/08");
            assertThat(ref.groups()).containsExactly(Map.entry("case_no", "II. ÚS 2379/08"));
        }

        @Test
        @DisplayName("case_no_gen reads the NSS / NS spelling")
        void caseNoGeneral() {
            Reference ref = only("Obdobně srov. rozsudek téhož soudu sp. zn. 6 Ads 45/2014.");

            assertThat(ref.kind()).isEqualTo(ReferenceKind.CASE_NO);
            assertThat(ref.rawText()).isEqualTo("sp. zn. 6 Ads 45/2014");
        }

        @Test
        @DisplayName("ecli captures the court, and the suffix stays ASCII")
        void ecli() {
            Reference ref = only("Rozhodnutí ECLI:CZ:NSS:2015:6.Ads.45.2014.32 bylo vydáno v roce 2015.");

            assertThat(ref.kind()).isEqualTo(ReferenceKind.ECLI);
            assertThat(ref.rawText()).isEqualTo("ECLI:CZ:NSS:2015:6.Ads.45.2014.32");
            assertThat(ref.groups()).containsExactly(Map.entry("court", "NSS"));
        }

        @Test
        @DisplayName("shared quirk: a sentence-final ECLI swallows the full stop")
        void ecliSwallowsASentenceFinalFullStop() {
            // Both runtimes do this — the suffix class in extract/patterns.toml allows a dot
            // and has no way to know the last one ends the sentence. Pinned rather than
            // worked around, because "fixing" it in Java alone would break parity and the
            // resolver is where a trailing dot has to be tolerated. If the pattern is ever
            // tightened, this assertion and the Python test change in the same commit.
            Reference ref = only("Rozhodnutí je evidováno pod ECLI:CZ:NSS:2015:6.Ads.45.2014.32.");

            assertThat(ref.rawText()).isEqualTo("ECLI:CZ:NSS:2015:6.Ads.45.2014.32.");
        }

        @Test
        @DisplayName("journal splits the R-číslo into sequence and year")
        void journalNumber() {
            Reference ref = only("Rozsudek byl publikován pod R 12/2015.");

            assertThat(ref.kind()).isEqualTo(ReferenceKind.JOURNAL_NO);
            assertThat(ref.rawText()).isEqualTo("R 12/2015");
            assertThat(ref.groups())
                    .containsExactlyInAnyOrderEntriesOf(
                            Map.of("journal_seq", "12", "journal_year", "2015"));
        }

        @Test
        @DisplayName("provision with an explicit act number fills all three groups")
        void provisionWithAct() {
            Reference ref = only("Soud posoudil věc podle § 2000 odst. 1 zákona č. 89/2012 Sb. a nárok přiznal.");

            assertThat(ref.kind()).isEqualTo(ReferenceKind.PROVISION);
            assertThat(ref.rawText()).isEqualTo("§ 2000 odst. 1 zákona č. 89/2012 Sb.");
            assertThat(ref.groups())
                    .containsExactlyInAnyOrderEntriesOf(
                            Map.of("section", "2000", "subsec", "1", "act_no", "89/2012"));
        }

        @Test
        @DisplayName("an anaphoric provision carries no act_no: the resolver supplies it, not the extractor")
        void provisionWithoutAct() {
            Reference ref = only("Odkaz na § 2000 odst. 1 zde nesměřuje k výslovně označenému zákonu.");

            assertThat(ref.rawText()).isEqualTo("§ 2000 odst. 1");
            assertThat(ref.groups())
                    .containsExactlyInAnyOrderEntriesOf(Map.of("section", "2000", "subsec", "1"));
            assertThat(ref.group("act_no")).isNull();
        }

        @Test
        @DisplayName("a trigger phrase alone matches nothing: that sentence is the model pass's input")
        void triggerPhraseIsNotAReference() {
            assertThat(extractor.extractParagraph(
                            "Soud odkázal na závěry citovaného rozhodnutí, aniž je blíže označil.", 1))
                    .isEmpty();
        }

        @Test
        @DisplayName("a bare prefix with no identifier after it matches nothing")
        void barePrefixIsNotAReference() {
            assertThat(extractor.extractParagraph(
                            "Podání neobsahovalo spisovou značku ani č. j.; datum je nesporné.", 1))
                    .isEmpty();
        }

        @Test
        @DisplayName("the plenum spelling matches: Pl. replaces the numeral, it does not prefix it")
        void plenumSpellingMatches() {
            // Was a recorded gap. PLAN.md section 7 wrote case_no_us as "(?:Pl\\.\\s*)?[IVX]+",
            // making "Pl." an optional prefix to a required numeral, so no plenary number
            // matched at all. Now an alternation. Both numbers below are real, read out of the
            // NALUS page cached under data/raw/US.
            assertThat(extractor.extractParagraph("Srov. též nález sp. zn. Pl. ÚS 29/98.", 1))
                    .singleElement()
                    .satisfies(r -> {
                        assertThat(r.kind()).isEqualTo(ReferenceKind.CASE_NO);
                        assertThat(r.groups()).containsEntry("case_no", "Pl. ÚS 29/98");
                    });
            assertThat(extractor.extractParagraph("Nález sp. zn. Pl. ÚS 36/93 na tom nic nemění.", 1))
                    .singleElement()
                    .satisfies(r -> assertThat(r.groups()).containsEntry("case_no", "Pl. ÚS 36/93"));
        }

        @Test
        @DisplayName("a bare ÚS with no panel designator still matches nothing")
        void bareUsWithoutDesignatorIsNotAReference() {
            // The alternation requires exactly one designator, so widening it for the plenum
            // did not cost precision here.
            assertThat(extractor.extractParagraph("Odkaz na sp. zn. ÚS 1/09 je zmatečný.", 1))
                    .isEmpty();
        }

        @Test
        @DisplayName("offsets index the paragraph body, and every span is verbatim")
        void offsetsAreVerbatim() {
            String body =
                    "Soud odkázal na rozsudek sp. zn. 6 Ads 45/2014, č. j. 6 Ads 45/2014-32, "
                            + "evidovaný pod ECLI:CZ:NSS:2015:6.Ads.45.2014.32 a publikovaný pod R 12/2015, "
                            + "a použil § 2000 odst. 1 zákona č. 89/2012 Sb.";

            List<Reference> found = extractor.extractParagraph(body, 7);

            assertThat(found)
                    .allSatisfy(
                            ref -> {
                                assertThat(body.substring(ref.start(), ref.end())).isEqualTo(ref.rawText());
                                assertThat(ref.paragraphIdx()).isEqualTo(7);
                            });
            assertThat(found).extracting(Reference::kind)
                    .containsExactly(
                            ReferenceKind.CASE_NO,
                            ReferenceKind.REF_NO,
                            ReferenceKind.ECLI,
                            ReferenceKind.JOURNAL_NO,
                            ReferenceKind.PROVISION);
        }
    }

    // ------------------------------------------------------------------ whole documents

    @Nested
    @DisplayName("extract(document)")
    class WholeDocument {

        @Test
        @DisplayName("numbers paragraphs from 1 and keeps offsets paragraph-relative")
        void paragraphNumbering() {
            String document =
                    """
                    Úvodní odstavec bez jakéhokoli odkazu.

                    Soud odkázal na č. j. 6 Ads 45/2014-32.

                    Dále použil § 2000 odst. 1 zákona č. 89/2012 Sb.
                    """;

            List<Reference> found = extractor.extract(document);

            assertThat(found).extracting(Reference::paragraphIdx).containsExactly(2, 3);
            assertThat(found.getFirst().start())
                    .as("offset into the paragraph, not into the document")
                    .isEqualTo("Soud odkázal na ".length());
        }

        @Test
        @DisplayName("a document with nothing in it yields nothing")
        void emptyDocument() {
            assertThat(extractor.extract("")).isEmpty();
            assertThat(extractor.extract("   \n\n \t ")).isEmpty();
        }

        @Test
        @DisplayName("non-breaking spaces are folded before any pattern runs")
        void nonBreakingSpacesAreFolded() {
            // Scraped court text is full of NBSP. Java's \\s is ASCII-only while Python's is
            // Unicode-aware, so folding in Paragraphs is what keeps the shared patterns honest.
            List<Reference> found = extractor.extract("Odkaz na č. j. 6 Ads 45/2014-32 v textu.");

            assertThat(found).singleElement()
                    .satisfies(ref -> assertThat(ref.rawText()).isEqualTo("č. j. 6 Ads 45/2014-32"));
        }
    }

    // ------------------------------------------------------------------ overlap policy

    @Nested
    @DisplayName("overlap policy")
    class OverlapPolicy {

        // Not citations: fabricated spans whose only job is to have bounds. TEST- prefixed so
        // they cannot resolve if one ever escapes into a fixture.
        private Reference span(int start, int end) {
            return new Reference(
                    ReferenceKind.CASE_NO, "TEST-" + start + "-" + end, start, end, 1, Map.of());
        }

        @Test
        @DisplayName("a span fully inside a longer one is dropped")
        void longestWins() {
            Reference outer = span(0, 30);
            Reference inner = span(10, 20);

            assertThat(CitationExtractor.dropContained(List.of(outer, inner))).containsExactly(outer);
            assertThat(CitationExtractor.dropContained(List.of(inner, outer))).containsExactly(outer);
        }

        @Test
        @DisplayName("spans that merely cross are both kept")
        void partialOverlapIsKept() {
            Reference left = span(0, 20);
            Reference right = span(10, 30);

            assertThat(CitationExtractor.dropContained(List.of(left, right)))
                    .containsExactly(left, right);
        }

        @Test
        @DisplayName("identical spans: the pattern declared first in the TOML wins")
        void firstDeclaredWinsOnIdenticalSpans() {
            Reference declaredFirst = span(5, 15);
            Reference declaredSecond = span(5, 15);

            assertThat(CitationExtractor.dropContained(List.of(declaredFirst, declaredSecond)))
                    .containsExactly(declaredFirst);
        }

        @Test
        @DisplayName("containment never crosses a paragraph boundary")
        void containmentIsPerParagraph() {
            Reference inParagraphOne = span(0, 30);
            Reference inParagraphTwo =
                    new Reference(ReferenceKind.CASE_NO, "TEST-other-paragraph", 10, 20, 2, Map.of());

            assertThat(CitationExtractor.dropContained(List.of(inParagraphOne, inParagraphTwo)))
                    .containsExactly(inParagraphOne, inParagraphTwo);
        }
    }

    // ------------------------------------------------------------------ paragraphs and windows

    @Nested
    @DisplayName("Paragraphs")
    class ParagraphHandling {

        @Test
        @DisplayName("splits on blank lines and normalises each paragraph")
        void splitOnBlankLines() {
            assertThat(Paragraphs.split("První   odstavec.\n\n\nDruhý\todstavec.\n"))
                    .containsExactly("První odstavec.", "Druhý odstavec.");
        }

        @Test
        @DisplayName("falls back to one paragraph per line when there is no blank line")
        void splitOnSingleNewlines() {
            assertThat(Paragraphs.split("První odstavec.\nDruhý odstavec.\nTřetí odstavec."))
                    .containsExactly("První odstavec.", "Druhý odstavec.", "Třetí odstavec.");
        }

        @Test
        @DisplayName("normalise folds every Unicode space Python's \\s matches")
        void normaliseFoldsUnicodeSpaces() {
            assertThat(Paragraphs.normalise(" a b　c \n d  "))
                    .isEqualTo("a b c d");
        }

        @Test
        @DisplayName("a context window is the paragraph plus two either side")
        void contextWindowInTheMiddle() {
            List<String> paragraphs = List.of("p1", "p2", "p3", "p4", "p5", "p6");

            ContextWindow window = Paragraphs.contextWindow(paragraphs, 4).orElseThrow();

            assertThat(window.centreIdx()).isEqualTo(4);
            assertThat(window.fromIdx()).isEqualTo(2);
            assertThat(window.toIdx()).isEqualTo(6);
            assertThat(window.paragraphs()).containsExactly("p2", "p3", "p4", "p5", "p6");
            assertThat(window.text()).isEqualTo("p2\n\np3\n\np4\n\np5\n\np6");
        }

        @Test
        @DisplayName("the edges truncate instead of failing: paragraph 1 has nothing before it")
        void contextWindowAtTheEdges() {
            List<String> paragraphs = List.of("p1", "p2", "p3", "p4");

            ContextWindow first = Paragraphs.contextWindow(paragraphs, 1).orElseThrow();
            ContextWindow last = Paragraphs.contextWindow(paragraphs, 4).orElseThrow();

            assertThat(first.fromIdx()).isEqualTo(1);
            assertThat(first.toIdx()).isEqualTo(3);
            assertThat(first.paragraphs()).containsExactly("p1", "p2", "p3");
            assertThat(last.fromIdx()).isEqualTo(2);
            assertThat(last.toIdx()).isEqualTo(4);
            assertThat(last.paragraphs()).containsExactly("p2", "p3", "p4");
        }

        @Test
        @DisplayName("a one-paragraph document still has a window")
        void contextWindowOfASingleParagraph() {
            ContextWindow only = Paragraphs.contextWindow(List.of("p1"), 1).orElseThrow();

            assertThat(only.centreIdx()).isEqualTo(1);
            assertThat(only.paragraphs()).containsExactly("p1");
        }

        @Test
        @DisplayName("one window per paragraph, each centred on its own")
        void windowsCoverEveryParagraph() {
            List<String> paragraphs = List.of("p1", "p2", "p3", "p4", "p5");

            assertThat(Paragraphs.contextWindows(paragraphs))
                    .hasSameSizeAs(paragraphs)
                    .extracting(ContextWindow::centreIdx)
                    .containsExactly(1, 2, 3, 4, 5);
        }

        @Test
        @DisplayName("an index outside the document has no window")
        void contextWindowOutOfRange() {
            assertThat(Paragraphs.contextWindow(List.of("p1"), 0)).isEmpty();
            assertThat(Paragraphs.contextWindow(List.of("p1"), 2)).isEmpty();
            assertThat(Paragraphs.contextWindows(List.of())).isEmpty();
        }

        @Test
        @DisplayName("batches chunk citations, last batch short rather than padded")
        void batching() {
            assertThat(Paragraphs.batches(List.of(1, 2, 3, 4, 5), 2))
                    .containsExactly(List.of(1, 2), List.of(3, 4), List.of(5));
            assertThat(Paragraphs.batches(List.<Integer>of(), 3)).isEmpty();
            assertThatThrownBy(() -> Paragraphs.batches(List.of(1), 0))
                    .isInstanceOf(IllegalArgumentException.class);
        }
    }

    // ------------------------------------------------------------------ kinds

    @Nested
    @DisplayName("ReferenceKind")
    class Kinds {

        @Test
        @DisplayName("the wire form round-trips, and it is what the TOML declares")
        void wireForm() {
            for (ReferenceKind kind : ReferenceKind.values()) {
                assertThat(ReferenceKind.fromWire(kind.wireName())).isEqualTo(kind);
            }
            assertThat(patterns.patterns())
                    .extracting(CompiledPattern::kind)
                    .isSubsetOf(List.of(ReferenceKind.values()));
        }

        @Test
        @DisplayName("an unknown kind in the TOML is a contract break, not something to skip")
        void unknownWireForm() {
            assertThatThrownBy(() -> ReferenceKind.fromWire("TEST-unknown-kind"))
                    .isInstanceOf(IllegalArgumentException.class)
                    .hasMessageContaining("TEST-unknown-kind");
        }
    }
}

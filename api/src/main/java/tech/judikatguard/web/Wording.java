package tech.judikatguard.web;

import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import tech.judikatguard.document.ReasonView;
import tech.judikatguard.status.Light;

/**
 * Every user-facing string the API produces. Czech, in the vocabulary of PLAN.md
 * section 16.
 *
 * <p>Czech law has no doctrine of binding precedent. A translated common-law term
 * ("overruled", "distinguished") is therefore not a rough translation but a substantive
 * mistake, and it will cost credibility with a legal audience inside the first minute. The
 * four words that carry the product are <em>zrušeno</em>, <em>překonáno</em>,
 * <em>argumentačně oslabeno</em> and <em>zúženo</em>.
 *
 * <p>Two things are never said. The system never says a source is <em>platný</em>: the
 * assertion is always scoped to a corpus and a date. And a failed classification is never
 * called <em>argumentačně oslabeno</em>, because that would assert something the evidence
 * does not support; it is called out for manual review instead.
 *
 * <p>This mirrors {@code web/src/czech.ts}, which owns the same wording for the page. The
 * duplication is deliberate: the API must be usable and honest without the UI in front
 * of it.
 */
public final class Wording {

    /** The GREEN phrasing required by CLAUDE.md rule 2. */
    public static final String NO_ADVERSE_TREATMENT = "nenalezeno žádné nepříznivé nakládání";

    public static final String BAD_REQUEST_TITLE = "Neplatný požadavek";

    public static final String NOT_FOUND_TITLE = "Zdroj nenalezen";

    public static final String LLM_UNAVAILABLE_TITLE = "Kontrola tvrzení není k dispozici";

    /** Headline word per reason kind. Order of severity, not of appearance. */
    private static final Map<String, String> HEADLINE = Map.ofEntries(
            Map.entry(ReasonView.QUASHED, "zrušeno"),
            Map.entry(ReasonView.SUPERSEDED, "překonáno"),
            Map.entry(ReasonView.PROVISION_DEROGATED, "zrušeno ustanovení"),
            Map.entry(ReasonView.CONFLICT, "argumentačně oslabeno"),
            Map.entry(ReasonView.NARROWED, "zúženo"),
            Map.entry(ReasonView.PROVISION_REWORDED, "argumentačně oslabeno"),
            Map.entry(ReasonView.UNCLASSIFIED, "vyžaduje ruční kontrolu"),
            Map.entry(ReasonView.INHERITED_WEAKNESS, "argumentačně oslabeno"));

    /** Evaluation order of PLAN.md section 9: which reason supplies the headline word. */
    private static final List<String> PRECEDENCE = List.of(
            ReasonView.QUASHED,
            ReasonView.SUPERSEDED,
            ReasonView.PROVISION_DEROGATED,
            ReasonView.CONFLICT,
            ReasonView.NARROWED,
            ReasonView.PROVISION_REWORDED,
            ReasonView.UNCLASSIFIED,
            ReasonView.INHERITED_WEAKNESS);

    private static final DateTimeFormatter CZECH_DATE =
            DateTimeFormatter.ofPattern("d. M. yyyy", Locale.forLanguageTag("cs"));

    private Wording() {}

    /** The headline phrase for a light and its reasons. Never the word "platný". */
    public static String phrase(Light light, List<ReasonView> reasons) {
        if (light == Light.GREEN || reasons.isEmpty()) {
            return NO_ADVERSE_TREATMENT;
        }
        for (String kind : PRECEDENCE) {
            for (ReasonView reason : reasons) {
                if (kind.equals(reason.kind())) {
                    return HEADLINE.get(kind);
                }
            }
        }
        // An unknown kind is still a real finding; it just has no phrase of its own yet.
        return light == Light.RED ? "překonáno" : "argumentačně oslabeno";
    }

    /**
     * The scope sentence of PLAN.md section 2. The whole point of the wording: the answer is
     * about one date and one corpus, and it never claims more than that.
     */
    public static String scopedVerdict(
            LocalDate asOf, int corpusSize, LocalDate corpusThrough, String phrase) {
        return "Posouzeno ke dni %s proti korpusu %d rozhodnutí zveřejněných do %s: %s."
                .formatted(date(asOf), corpusSize, date(corpusThrough), phrase);
    }

    public static String date(LocalDate date) {
        return CZECH_DATE.format(date);
    }
}

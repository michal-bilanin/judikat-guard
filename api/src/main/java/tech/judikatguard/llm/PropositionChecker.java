package tech.judikatguard.llm;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.LocalDate;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.TreeSet;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * "Are you using this decision correctly?" PLAN.md section 12, M7.
 *
 * <p>Runs {@code prompts/proposition-check.v1.md} at query time: the cited decision's
 * holding and passages against the claim the user's document offers it for. The model
 * labels the relationship; this class enforces the two rules that make the label usable.
 *
 * <ol>
 *   <li>The reply must quote the material it was shown. {@link EvidenceSpan} decides, over
 *       the holding and the passages, and nothing else counts as evidence. A reply that
 *       fails is sent back once with the violation stated; a second failure returns
 *       {@link PropositionVerdict#unclassified} and stores nothing (CLAUDE.md rule 3).</li>
 *   <li>Every accepted reply is cached with the model and the prompt version that produced
 *       it, on the key the prompt's own frontmatter documents.</li>
 * </ol>
 *
 * <p>Not thread-confined: holds no mutable state, so one instance serves every request.
 * {@link LlmClient.Unavailable} propagates to the caller, which reports the source as
 * unchecked; that is a different outcome from {@code UNCLASSIFIED}, where a reply arrived and
 * could not be trusted.
 */
public final class PropositionChecker {

    public static final String PROMPT_FILE = "proposition-check.v1.md";

    private static final Logger log = LoggerFactory.getLogger(PropositionChecker.class);

    private final LlmClient llm;
    private final LlmCacheRepository cache;
    private final ObjectMapper json;
    private final PromptTemplate template;

    /** Loads the prompt and builds its own mapper. Convenient for tests and for the CLI. */
    public PropositionChecker(LlmClient llm, LlmCacheRepository cache) {
        this(llm, cache, JsonMapper.builder().build(), PromptTemplate.load(PROMPT_FILE));
    }

    /** The wiring constructor: takes the mapper Spring Boot already configured. */
    public PropositionChecker(
            LlmClient llm, LlmCacheRepository cache, ObjectMapper json, PromptTemplate template) {
        this.llm = Objects.requireNonNull(llm, "llm");
        this.cache = Objects.requireNonNull(cache, "cache");
        this.json = Objects.requireNonNull(json, "json");
        this.template = Objects.requireNonNull(template, "template");
    }

    /**
     * @param citedEcli the decision as resolved in the corpus
     * @param citedCourt {@code US} | {@code NSS} | {@code NS}
     * @param citedDate {@code decision.decided_on}
     * @param citedRatio the {@code právní věta} or {@code ratio_summary}; pass an empty
     *     string when the decision has none, which is a normal state
     * @param citedContext the paragraphs shown to the model, joined by blank lines
     * @param claim what the user's document offers the decision for
     */
    public PropositionVerdict check(
            String citedEcli,
            String citedCourt,
            LocalDate citedDate,
            String citedRatio,
            String citedContext,
            String claim) {

        String prompt = template.render(Map.of(
                "cited_ecli", citedEcli,
                "cited_court", citedCourt,
                "cited_date", citedDate.toString(),
                "cited_ratio", citedRatio,
                "cited_context", citedContext,
                "claim", claim));
        String cacheKey = cacheKey(template.version(), citedEcli, claim);

        Optional<LlmCacheRepository.CachedCall> hit = cache.find(cacheKey);
        if (hit.isPresent()) {
            // Re-validated rather than trusted: the documented key covers the decision and
            // the claim but not the passages, so a re-crawled decision can leave a stored
            // reply quoting text that is no longer there. Then it is not evidence any more.
            Parsed replay = parse(hit.get().response(), citedRatio, citedContext);
            if (replay instanceof Parsed.Ok(PropositionVerdict verdict)) {
                return verdict;
            }
            log.warn(
                    "llm_cache {} no longer validates ({}), calling the model again",
                    cacheKey,
                    ((Parsed.Rejected) replay).violation());
        }

        Parsed first = parse(llm.complete(prompt), citedRatio, citedContext);
        if (first instanceof Parsed.Ok(PropositionVerdict verdict)) {
            store(cacheKey, prompt, verdict);
            return verdict;
        }
        String violation = ((Parsed.Rejected) first).violation();
        log.info("proposition check for {} rejected, retrying once: {}", citedEcli, violation);

        // Exactly one retry, with the violation stated back. PLAN.md section 8.
        String retryPrompt = prompt + correction(violation);
        Parsed second = parse(llm.complete(retryPrompt), citedRatio, citedContext);
        if (second instanceof Parsed.Ok(PropositionVerdict verdict)) {
            store(cacheKey, retryPrompt, verdict);
            return verdict;
        }
        log.warn(
                "proposition check for {} unclassified after one retry: {}",
                citedEcli,
                ((Parsed.Rejected) second).violation());
        return PropositionVerdict.unclassified(
                "Ověření tvrzení se nezdařilo: odpověď modelu neobsahovala doslovnou citaci "
                        + "z rozhodnutí. Posuďte použití rozhodnutí sami.");
    }

    /**
     * {@code (cited_ecli, claim_hash, prompt_version)}, the tuple documented in the prompt's
     * frontmatter. The claim is whitespace-normalised before hashing so that a reflowed
     * paste of the same sentence still hits.
     */
    static String cacheKey(String promptVersion, String citedEcli, String claim) {
        return promptVersion + "|" + citedEcli + "|" + sha256(EvidenceSpan.normalizeWhitespace(claim));
    }

    private void store(String cacheKey, String prompt, PropositionVerdict verdict) {
        Map<String, Object> request = new LinkedHashMap<>();
        request.put("prompt_version", template.version());
        request.put("prompt", prompt);

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("verdict", verdict.verdict());
        response.put("confidence", verdict.confidence());
        response.put("evidence_span", verdict.evidenceSpan());
        response.put("note", verdict.note());

        cache.put(new LlmCacheRepository.CachedCall(
                cacheKey,
                template.version(),
                llm.model(),
                json.writeValueAsString(request),
                json.writeValueAsString(response)));
    }

    /**
     * Validates one reply. Every rejection carries the sentence that goes back to the model,
     * so a bad shape, a bad verdict and a fabricated quote all take the same single retry.
     */
    private Parsed parse(String reply, String citedRatio, String citedContext) {
        String object = jsonObjectIn(reply);
        if (object == null) {
            return new Parsed.Rejected("your reply contained no JSON object");
        }
        JsonNode node;
        try {
            node = json.readTree(object);
        } catch (JacksonException e) {
            return new Parsed.Rejected("your reply was not valid JSON: " + e.getOriginalMessage());
        }
        String verdict = node.path("verdict").asString("").strip();
        if (!PropositionVerdict.VERDICTS.contains(verdict)) {
            return new Parsed.Rejected(
                    "`verdict` must be exactly one of " + new TreeSet<>(PropositionVerdict.VERDICTS)
                            + ", and yours was \"" + verdict + "\"");
        }
        double confidence = node.path("confidence").asDouble(-1.0);
        if (!Double.isFinite(confidence) || confidence < 0.0 || confidence > 1.0) {
            return new Parsed.Rejected("`confidence` must be a number in [0, 1]");
        }
        String span = node.path("evidence_span").asString("");
        if (span.isBlank()) {
            return new Parsed.Rejected("`evidence_span` was empty");
        }
        if (!EvidenceSpan.isPresentIn(span, citedRatio, citedContext)) {
            return new Parsed.Rejected(
                    "`evidence_span` is not a verbatim substring of the holding or the passages "
                            + "you were given; it read \"" + span + "\"");
        }
        return new Parsed.Ok(new PropositionVerdict(
                verdict, confidence, span, node.path("note").asString("")));
    }

    /**
     * The outermost {@code { ... }} in the reply, or null. Tolerating a fenced code block or
     * a leading sentence is transport robustness and costs nothing; the verdict, the range of
     * the confidence and the quote are all still checked afterwards.
     */
    private static @Nullable String jsonObjectIn(String reply) {
        int start = reply.indexOf('{');
        int end = reply.lastIndexOf('}');
        return start < 0 || end <= start ? null : reply.substring(start, end + 1);
    }

    /**
     * The retry turn. Same prompt version: this restates the contract the prompt already
     * sets, it does not change it, so nothing here needs a new prompt file (CLAUDE.md rule 8).
     */
    private static String correction(String violation) {
        return """


                ## Correction

                Your previous reply was rejected: %s.

                Return one JSON object and nothing else, in exactly the shape given above.
                `evidence_span` must be copied character for character out of the holding or the
                passages in this prompt: do not paraphrase it, translate it, fix its spelling or
                join words across a line break. If no passage in this prompt carries your
                verdict, the honest answer is `UNRELATED` with a quote showing that the decision
                does not address the claim.
                """.formatted(violation);
    }

    private static String sha256(String value) {
        try {
            return HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is required of every JVM", e);
        }
    }

    /** A reply either became a verdict or produced the sentence explaining why it did not. */
    private sealed interface Parsed {

        record Ok(PropositionVerdict verdict) implements Parsed {}

        record Rejected(String violation) implements Parsed {}
    }
}

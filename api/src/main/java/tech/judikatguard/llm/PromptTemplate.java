package tech.judikatguard.llm;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.jspecify.annotations.Nullable;

/**
 * One versioned prompt file from {@code prompts/}, parsed and renderable. D7 and PLAN.md
 * section 8.
 *
 * <p>The files are shared with the Python pipeline and are frozen once a row references
 * their version (CLAUDE.md rule 8), so {@link #version()} is read out of the frontmatter and
 * never derived, defaulted or hardcoded. Every {@code treatment},
 * {@code provision_materiality} and {@code llm_cache} row records that string; a wrong one
 * silently detaches stored labels from the prompt that produced them.
 *
 * <p>File shape, as written by all three current prompts:
 *
 * <pre>
 * ---
 * version: proposition-check.v1
 * cache_key: (cited_ecli, claim_hash, prompt_version)
 * temperature: 0
 * placeholders:
 *   - cited_ecli
 *   - claim
 * ---
 *
 * &lt;!-- notes to implementers, stripped --&gt;
 *
 * Markdown with {{cited_ecli}} placeholders.
 * </pre>
 *
 * <p>Two deliberate strictnesses:
 *
 * <ul>
 *   <li>{@code <!-- ... -->} blocks are removed with the frontmatter. They are notes to
 *       whoever edits the file, they discuss {@code {{placeholders}}} in prose, and sending
 *       them to the model is noise at best.</li>
 *   <li>{@link #render} refuses to produce a partially substituted prompt, and parsing
 *       refuses a file whose declared {@code placeholders} do not match the ones in its body.
 *       A prompt with a literal {@code {{context}}} left in it still returns a confident,
 *       well-formed, entirely worthless label. Crashing is the cheaper outcome.</li>
 * </ul>
 */
public final class PromptTemplate {

    /** Literal {@code {{name}}}. No inner whitespace, no expressions: this is not a DSL. */
    private static final Pattern PLACEHOLDER = Pattern.compile("\\{\\{([A-Za-z0-9_]+)}}");

    private static final Pattern HTML_COMMENT = Pattern.compile("<!--.*?-->", Pattern.DOTALL);

    private static final String FENCE = "---";

    /** Overrides the search below, for a deployment whose layout is not the repository's. */
    private static final String DIR_PROPERTY = "jg.prompts.dir";

    private static final String DIR_ENV = "JG_PROMPTS_DIR";

    private final String name;
    private final String version;
    private final @Nullable String cacheKey;
    private final String body;
    private final Set<String> placeholders;

    private PromptTemplate(
            String name,
            String version,
            @Nullable String cacheKey,
            String body,
            Set<String> placeholders) {
        this.name = name;
        this.version = version;
        this.cacheKey = cacheKey;
        this.body = body;
        this.placeholders = Set.copyOf(placeholders);
    }

    /**
     * Loads {@code fileName} from the shared {@code prompts/} directory.
     *
     * @param fileName for example {@code proposition-check.v1.md}
     */
    public static PromptTemplate load(String fileName) {
        return load(promptsDir().resolve(fileName));
    }

    public static PromptTemplate load(Path file) {
        try {
            return parse(file.getFileName().toString(), Files.readString(file, StandardCharsets.UTF_8));
        } catch (IOException e) {
            throw new UncheckedIOException("cannot read prompt " + file.toAbsolutePath(), e);
        }
    }

    /**
     * The repository's {@code prompts/} directory: {@code -Djg.prompts.dir}, else
     * {@code $JG_PROMPTS_DIR}, else the nearest {@code prompts} directory at or above the
     * working directory. The search exists because Maven runs the application and the tests
     * from {@code api/} while the prompts live one level up, shared with the pipeline.
     */
    public static Path promptsDir() {
        String configured = System.getProperty(DIR_PROPERTY, System.getenv(DIR_ENV));
        if (configured != null && !configured.isBlank()) {
            return Path.of(configured);
        }
        Path start = Path.of("").toAbsolutePath();
        for (Path dir = start; dir != null; dir = dir.getParent()) {
            Path candidate = dir.resolve("prompts");
            if (Files.isDirectory(candidate)) {
                return candidate;
            }
        }
        throw new IllegalStateException(
                "no prompts/ directory at or above " + start + "; set -D" + DIR_PROPERTY);
    }

    /**
     * @param name the file name, used only in error messages
     * @param source the whole file, frontmatter included
     */
    public static PromptTemplate parse(String name, String source) {
        Objects.requireNonNull(name, "name");
        Objects.requireNonNull(source, "source");

        Frontmatter front = splitFrontmatter(name, source);
        String version = front.scalars().get("version");
        if (version == null || version.isBlank()) {
            throw new IllegalArgumentException(
                    "prompt " + name + " has no `version:` in its frontmatter; every stored row "
                            + "records prompt_version, so it cannot be defaulted");
        }
        String body = HTML_COMMENT.matcher(front.body()).replaceAll("").strip();
        Set<String> found = placeholdersIn(body);

        List<String> declared = front.lists().get("placeholders");
        if (declared != null) {
            Set<String> declaredSet = new TreeSet<>(declared);
            if (!declaredSet.equals(new TreeSet<>(found))) {
                throw new IllegalArgumentException(
                        "prompt " + name + " declares placeholders " + declaredSet
                                + " but its body uses " + new TreeSet<>(found));
            }
        }
        return new PromptTemplate(
                name, version.strip(), front.scalars().get("cache_key"), body, found);
    }

    /** The file name, for example {@code proposition-check.v1.md}. */
    public String name() {
        return name;
    }

    /**
     * The frontmatter {@code version:} value, for example {@code proposition-check.v1}.
     * Written to the {@code prompt_version} column of every row derived from this prompt.
     */
    public String version() {
        return version;
    }

    /**
     * The frontmatter {@code cache_key:} value: the documented tuple a caller must build its
     * {@code llm_cache.cache_key} from. Documentation for the caller, not machinery; null
     * when the prompt declares none.
     */
    public @Nullable String cacheKey() {
        return cacheKey;
    }

    /** The prompt with frontmatter and implementer comments removed, placeholders intact. */
    public String body() {
        return body;
    }

    /** The placeholder names this prompt requires, sorted. */
    public Set<String> placeholders() {
        return new TreeSet<>(placeholders);
    }

    /**
     * Substitutes every {@code {{name}}} with {@code values.get(name)}.
     *
     * <p>All or nothing. A missing key or an unknown key is a programming error and throws
     * before anything is sent. Values are inserted literally: a value that itself contains
     * {@code {{...}}} is not re-scanned.
     *
     * @throws IllegalArgumentException if any placeholder is unsupplied, or any supplied key
     *     is not a placeholder of this prompt
     */
    public String render(Map<String, String> values) {
        Set<String> missing = new TreeSet<>(placeholders);
        missing.removeAll(values.keySet());
        if (!missing.isEmpty()) {
            throw new IllegalArgumentException(
                    "prompt " + name + " would render with unsubstituted placeholders " + missing);
        }
        Set<String> unknown = new TreeSet<>(values.keySet());
        unknown.removeAll(placeholders);
        if (!unknown.isEmpty()) {
            throw new IllegalArgumentException(
                    "prompt " + name + " has no placeholders " + unknown
                            + "; it takes " + new TreeSet<>(placeholders));
        }
        Matcher matcher = PLACEHOLDER.matcher(body);
        StringBuilder rendered = new StringBuilder(body.length());
        while (matcher.find()) {
            String value = values.get(matcher.group(1));
            matcher.appendReplacement(rendered, Matcher.quoteReplacement(Objects.toString(value, "")));
        }
        matcher.appendTail(rendered);
        return rendered.toString();
    }

    private static Set<String> placeholdersIn(String body) {
        Set<String> found = new TreeSet<>();
        Matcher matcher = PLACEHOLDER.matcher(body);
        while (matcher.find()) {
            found.add(matcher.group(1));
        }
        return found;
    }

    /**
     * Minimal YAML: {@code key: value} scalars and {@code - item} lists, which is all the
     * three prompt files use and all they are allowed to use. Nested maps, quoting rules,
     * anchors and multi-line scalars are not supported, on purpose: adding a YAML parser to
     * read four keys is a dependency this project does not need.
     */
    private static Frontmatter splitFrontmatter(String name, String source) {
        String normalised = source.replace("\r\n", "\n");
        if (!normalised.startsWith(FENCE + "\n")) {
            throw new IllegalArgumentException(
                    "prompt " + name + " does not start with a `---` frontmatter block");
        }
        int end = normalised.indexOf("\n" + FENCE, FENCE.length());
        if (end < 0) {
            throw new IllegalArgumentException(
                    "prompt " + name + " has an unterminated frontmatter block");
        }
        String header = normalised.substring(FENCE.length() + 1, end);
        int bodyStart = normalised.indexOf('\n', end + 1);
        String body = bodyStart < 0 ? "" : normalised.substring(bodyStart + 1);

        Map<String, String> scalars = new LinkedHashMap<>();
        Map<String, List<String>> lists = new LinkedHashMap<>();
        String currentKey = null;
        for (String line : header.split("\n", -1)) {
            if (line.isBlank() || line.strip().startsWith("#")) {
                continue;
            }
            String stripped = line.strip();
            if (stripped.startsWith("- ")) {
                if (currentKey == null) {
                    throw new IllegalArgumentException(
                            "prompt " + name + " frontmatter has a list item before any key");
                }
                lists.computeIfAbsent(currentKey, k -> new ArrayList<>()).add(stripped.substring(2).strip());
                continue;
            }
            int colon = stripped.indexOf(':');
            if (colon < 0) {
                throw new IllegalArgumentException(
                        "prompt " + name + " frontmatter line is neither `key: value` nor `- item`: "
                                + stripped);
            }
            currentKey = stripped.substring(0, colon).strip();
            String value = stripped.substring(colon + 1).strip();
            if (!value.isEmpty()) {
                scalars.put(currentKey, value);
            }
        }
        return new Frontmatter(scalars, lists, body);
    }

    private record Frontmatter(
            Map<String, String> scalars, Map<String, List<String>> lists, String body) {}
}

package tech.judikatguard.extract;

import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.Reader;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;
import org.jspecify.annotations.Nullable;
import org.tomlj.Toml;
import org.tomlj.TomlArray;
import org.tomlj.TomlParseError;
import org.tomlj.TomlParseResult;
import org.tomlj.TomlPosition;
import org.tomlj.TomlTable;

/**
 * Everything {@code extract/patterns.toml} declares, compiled once.
 *
 * <p>That file is the single source of the citation regexes (PLAN.md section 7) and this
 * class is the only place the Java runtime reads them. No regex is written out in Java
 * anywhere; a copy here would drift from the Python pipeline and the parity fixture would
 * catch it a day later than it should.
 *
 * <p>Two things are deliberate:
 *
 * <ul>
 *   <li>Patterns are compiled with <strong>no flags</strong>. In particular not
 *       {@link Pattern#UNICODE_CHARACTER_CLASS}: the patterns are written for the ASCII
 *       reading of {@code \w} / {@code \d} / {@code \s}, and turning that flag on would make
 *       Java match strings Python does not.
 *   <li>Pattern order is the declaration order in the TOML, recovered from the source
 *       position of each table header rather than from map iteration. Order is the tie-break
 *       in the overlap policy (see {@link CitationExtractor}), so it has to be as stable and
 *       reviewable as the Python side, where {@code tomllib} hands back insertion order.
 * </ul>
 */
public final class PatternSet {

    /** System property holding an explicit path to {@code patterns.toml}. */
    public static final String PATH_PROPERTY = "jg.patterns.toml";

    /** Repo-relative location, and the classpath resource name of a packaged copy. */
    public static final String RELATIVE_PATH = "extract/patterns.toml";

    private static final String CLASSPATH_KEY = "classpath:" + RELATIVE_PATH;

    /** One instance per resolved location. Compiling the file per request is pointless. */
    private static final Map<String, PatternSet> CACHE = new ConcurrentHashMap<>();

    private final int version;
    private final String origin;
    private final List<CompiledPattern> patterns;
    private final List<String> citationTriggers;
    private final Map<String, List<String>> markers;

    private PatternSet(
            int version,
            String origin,
            List<CompiledPattern> patterns,
            List<String> citationTriggers,
            Map<String, List<String>> markers) {
        this.version = version;
        this.origin = origin;
        this.patterns = List.copyOf(patterns);
        this.citationTriggers = List.copyOf(citationTriggers);
        this.markers = Map.copyOf(markers);
    }

    /**
     * One {@code [patterns.*]} table, compiled.
     *
     * @param name   the TOML table name, e.g. {@code case_no_us}
     * @param kind   the {@link ReferenceKind} every match of this pattern carries
     * @param regex  compiled with no flags, see the class javadoc
     * @param groups capture-group names, positional: {@code groups.get(n - 1)} names group
     *               {@code n}
     */
    public record CompiledPattern(
            String name, ReferenceKind kind, Pattern regex, List<String> groups) {

        public CompiledPattern {
            Objects.requireNonNull(name, "name");
            Objects.requireNonNull(kind, "kind");
            Objects.requireNonNull(regex, "regex");
            groups = List.copyOf(groups);
        }

        /**
         * Every match of this one pattern in {@code text}, in text order, before the overlap
         * policy is applied.
         */
        public List<Reference> find(String text, int paragraphIdx) {
            List<Reference> found = new ArrayList<>();
            var matcher = regex.matcher(text);
            while (matcher.find()) {
                found.add(
                        new Reference(
                                kind,
                                matcher.group(),
                                matcher.start(),
                                matcher.end(),
                                paragraphIdx,
                                namedGroups(matcher)));
            }
            return found;
        }

        /**
         * Map capture group {@code n} to {@code groups.get(n - 1)}, skipping groups that did
         * not participate in the match. Mirrors {@code CompiledPattern._named_groups}.
         */
        private Map<String, String> namedGroups(java.util.regex.MatchResult match) {
            Map<String, String> named = new LinkedHashMap<>();
            for (int position = 1; position <= groups.size(); position++) {
                if (position > match.groupCount()) {
                    break;
                }
                String value = match.group(position);
                if (value != null) {
                    named.put(groups.get(position - 1), value);
                }
            }
            return named;
        }
    }

    // ------------------------------------------------------------------ loading

    /** Parse and compile one {@code patterns.toml}. Uncached; see {@link #fromClasspathOrRepo()}. */
    public static PatternSet load(Path tomlPath) {
        Objects.requireNonNull(tomlPath, "tomlPath");
        TomlParseResult toml;
        try {
            toml = Toml.parse(tomlPath);
        } catch (IOException e) {
            throw new UncheckedIOException("cannot read " + tomlPath, e);
        }
        return fromToml(toml, tomlPath.toString());
    }

    /**
     * The shared {@code extract/patterns.toml}, resolved in this order:
     *
     * <ol>
     *   <li>the {@value #PATH_PROPERTY} system property, so a test never depends on the
     *       working directory;
     *   <li>the nearest {@code extract/patterns.toml} found by walking up from the working
     *       directory, which is how it resolves in the repo and under Maven;
     *   <li>the classpath resource {@value #RELATIVE_PATH}, for a packaged jar.
     * </ol>
     *
     * <p>The repo file wins over a packaged copy on purpose: a stale copy inside the jar is
     * a divergence the parity test cannot see.
     *
     * @throws IllegalStateException if no copy can be found, or the file does not parse
     */
    public static PatternSet fromClasspathOrRepo() {
        String override = System.getProperty(PATH_PROPERTY);
        if (override != null && !override.isBlank()) {
            Path path = Path.of(override).toAbsolutePath().normalize();
            if (!Files.isRegularFile(path)) {
                throw new IllegalStateException(
                        PATH_PROPERTY + " points at " + path + ", which is not a readable file");
            }
            return CACHE.computeIfAbsent(path.toString(), key -> load(path));
        }
        Path inRepo = findInRepo();
        if (inRepo != null) {
            return CACHE.computeIfAbsent(inRepo.toString(), key -> load(inRepo));
        }
        return CACHE.computeIfAbsent(CLASSPATH_KEY, key -> loadFromClasspath());
    }

    /** The nearest {@code extract/patterns.toml} at or above the working directory. */
    static @Nullable Path findInRepo() {
        Path directory = Path.of("").toAbsolutePath().normalize();
        while (directory != null) {
            Path candidate = directory.resolve(RELATIVE_PATH);
            if (Files.isRegularFile(candidate)) {
                return candidate;
            }
            directory = directory.getParent();
        }
        return null;
    }

    private static PatternSet loadFromClasspath() {
        ClassLoader loader = PatternSet.class.getClassLoader();
        try (InputStream stream = loader.getResourceAsStream(RELATIVE_PATH)) {
            if (stream == null) {
                throw new IllegalStateException(
                        "no "
                                + RELATIVE_PATH
                                + " on the classpath and none found above "
                                + Path.of("").toAbsolutePath()
                                + "; set -D"
                                + PATH_PROPERTY
                                + "=/path/to/patterns.toml");
            }
            try (Reader reader = new InputStreamReader(stream, StandardCharsets.UTF_8)) {
                return fromToml(Toml.parse(reader), CLASSPATH_KEY);
            }
        } catch (IOException e) {
            throw new UncheckedIOException("cannot read " + CLASSPATH_KEY, e);
        }
    }

    private static PatternSet fromToml(TomlParseResult toml, String origin) {
        if (toml.hasErrors()) {
            StringBuilder message = new StringBuilder(origin + " does not parse:");
            for (TomlParseError error : toml.errors()) {
                message.append("\n  ").append(error.toString());
            }
            throw new IllegalStateException(message.toString());
        }

        TomlTable patternsTable = toml.getTable("patterns");
        if (patternsTable == null || patternsTable.isEmpty()) {
            throw new IllegalStateException(origin + " declares no [patterns.*] tables");
        }

        Comparator<String> byDeclaration =
                Comparator.comparingLong(name -> declarationOrder(patternsTable, name));
        List<String> names = new ArrayList<>(patternsTable.keySet());
        names.sort(byDeclaration);

        List<CompiledPattern> compiled = new ArrayList<>(names.size());
        for (String name : names) {
            TomlTable table = patternsTable.getTable(name);
            if (table == null) {
                throw new IllegalStateException(origin + ": [patterns." + name + "] is not a table");
            }
            compiled.add(compile(origin, name, table));
        }

        Long declaredVersion = toml.getLong("version");
        return new PatternSet(
                declaredVersion == null ? 0 : declaredVersion.intValue(),
                origin,
                compiled,
                stringList(toml.getArray("triggers.citation")),
                markerTables(toml.getTable("markers")));
    }

    private static CompiledPattern compile(String origin, String name, TomlTable table) {
        String regex = table.getString("regex");
        String kind = table.getString("kind");
        if (regex == null || kind == null) {
            throw new IllegalStateException(
                    origin + ": [patterns." + name + "] needs both 'regex' and 'kind'");
        }
        try {
            // No flags. See the class javadoc: UNICODE_CHARACTER_CLASS would break parity.
            return new CompiledPattern(
                    name,
                    ReferenceKind.fromWire(kind),
                    Pattern.compile(regex),
                    stringList(table.getArray("groups")));
            // PatternSyntaxException (a bad regex) and IllegalArgumentException (an unknown
            // kind) are both IllegalArgumentException; either way the file is unusable.
        } catch (IllegalArgumentException e) {
            throw new IllegalStateException(
                    origin + ": [patterns." + name + "] is unusable: " + e.getMessage(), e);
        }
    }

    private static Map<String, List<String>> markerTables(@Nullable TomlTable markers) {
        Map<String, List<String>> byName = new LinkedHashMap<>();
        if (markers == null) {
            return byName;
        }
        for (String name : markers.keySet()) {
            TomlArray array = markers.getArray(name);
            if (array != null && isStringArray(array)) {
                byName.put(name, stringList(array));
            }
        }
        return byName;
    }

    private static boolean isStringArray(TomlArray array) {
        for (int i = 0; i < array.size(); i++) {
            if (!(array.get(i) instanceof String)) {
                return false;
            }
        }
        return true;
    }

    private static List<String> stringList(@Nullable TomlArray array) {
        if (array == null) {
            return List.of();
        }
        List<String> values = new ArrayList<>(array.size());
        for (int i = 0; i < array.size(); i++) {
            if (array.get(i) instanceof String value) {
                values.add(value);
            } else {
                throw new IllegalStateException(
                        "expected an array of strings, element " + i + " is not a string");
            }
        }
        return values;
    }

    /**
     * Sort key recovering TOML declaration order: line in the high half, column in the low
     * half. A key whose source position tomlj cannot report sorts last rather than throwing,
     * because losing the tie-break order is worse than a slightly odd order in that case.
     */
    private static long declarationOrder(TomlTable table, String key) {
        TomlPosition position = table.inputPositionOf(key);
        if (position == null) {
            return Long.MAX_VALUE;
        }
        return ((long) position.line() << 32) | (position.column() & 0xffffffffL);
    }

    // ------------------------------------------------------------------ accessors

    /** The {@code version} key of the TOML. Bumped when the pattern contract changes. */
    public int version() {
        return version;
    }

    /** Where this set was loaded from. Shown in error messages, not parsed. */
    public String origin() {
        return origin;
    }

    /** Compiled patterns in TOML declaration order, which is the overlap tie-break order. */
    public List<CompiledPattern> patterns() {
        return patterns;
    }

    /**
     * One pattern by TOML table name.
     *
     * @throws IllegalArgumentException if no pattern carries that name
     */
    public CompiledPattern byName(String name) {
        for (CompiledPattern pattern : patterns) {
            if (pattern.name().equals(name)) {
                return pattern;
            }
        }
        throw new IllegalArgumentException("no pattern named '" + name + "' in " + origin);
    }

    /**
     * {@code [triggers].citation}: phrases such as <em>citovaného rozhodnutí</em> or
     * <em>tamtéž</em>. A sentence containing one of these but yielding no rule match is the
     * only input to the anaphora model pass (PLAN.md section 7).
     */
    public List<String> citationTriggers() {
        return citationTriggers;
    }

    /**
     * {@code [markers].party_submission}: a citation inside a passage reporting what a party
     * argued is not the court relying on it, so the structural router labels it
     * {@code MENTIONED} without a model call (PLAN.md section 8, tier 1).
     */
    public List<String> partySubmissionMarkers() {
        return markers("party_submission");
    }

    /**
     * {@code [markers].departure}: phrases that force escalation to the reasoning model
     * (PLAN.md section 8, tier 2).
     */
    public List<String> departureMarkers() {
        return markers("departure");
    }

    /**
     * {@code [markers].quashing}: the operative-part wording that annuls another decision,
     * the {@code QUASHED} signal (PLAN.md section 8, tier 1).
     */
    /**
     * {@code [markers].narrowing}: scope-limiting wording that forces escalation for a
     * possible {@code NARROWED}, as distinct from a departure. Kept separate from
     * {@link #departureMarkers()} because the two vocabularies do not overlap — a court
     * narrowing a rule is not departing from it — and because {@code NARROWED} is an amber
     * row of PLAN.md section 9 while {@code DISTINGUISHED} is not, which is why
     * distinguishing vocabulary is deliberately absent from the list.
     */
    public List<String> narrowingMarkers() {
        return markers("narrowing");
    }

    public List<String> quashingMarkers() {
        return markers("quashing");
    }

    /** One {@code [markers]} list by name, empty when the TOML declares no such list. */
    public List<String> markers(String name) {
        return markers.getOrDefault(name, List.of());
    }

    @Override
    public String toString() {
        return "PatternSet[version=" + version + ", patterns=" + patterns.size() + ", origin=" + origin + "]";
    }
}

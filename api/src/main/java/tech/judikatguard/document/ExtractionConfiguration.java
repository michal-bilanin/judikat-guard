package tech.judikatguard.document;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import tech.judikatguard.extract.CitationExtractor;
import tech.judikatguard.extract.PatternSet;

/**
 * Wires the query-time extractor.
 *
 * <p>{@link CitationExtractor} carries no {@code @Component}: its pattern set is a loaded
 * file shared with the Python pipeline, not a component to be discovered. Loading it here
 * means a missing or malformed {@code extract/patterns.toml} takes the application down at
 * startup instead of failing the first upload — the two runtimes disagreeing about what a
 * citation looks like is a defect that must not be discoverable only in a demo.
 */
@Configuration(proxyBeanMethods = false)
public class ExtractionConfiguration {

    @Bean
    PatternSet patternSet() {
        return PatternSet.fromClasspathOrRepo();
    }

    @Bean
    CitationExtractor citationExtractor(PatternSet patterns) {
        return new CitationExtractor(patterns);
    }
}

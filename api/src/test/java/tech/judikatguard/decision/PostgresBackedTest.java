package tech.judikatguard.decision;

import org.junit.jupiter.api.Tag;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.junit.jupiter.Testcontainers;

/**
 * Base class for the tests that need a real Postgres, real Flyway migrations and a real
 * Spring context.
 *
 * <p>{@code disabledWithoutDocker = true} is the "skip loudly" behaviour: on a machine with
 * no Docker socket the whole class is reported as skipped, with a reason, rather than
 * failing the build. The rules-engine truth table and the extraction parity test do not
 * need a container and still run.
 *
 * <p>Every subclass shares one container and, as long as it adds no properties of its own,
 * one cached Spring context.
 */
@SpringBootTest
@Testcontainers(disabledWithoutDocker = true)
@Tag("postgres")
abstract class PostgresBackedTest {

    @DynamicPropertySource
    static void datasource(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", SharedPostgres.get()::getJdbcUrl);
        registry.add("spring.datasource.username", SharedPostgres.get()::getUsername);
        registry.add("spring.datasource.password", SharedPostgres.get()::getPassword);
    }
}

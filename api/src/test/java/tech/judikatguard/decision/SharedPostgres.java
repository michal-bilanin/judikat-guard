package tech.judikatguard.decision;

import org.jspecify.annotations.Nullable;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

/**
 * One Postgres container for the whole test run.
 *
 * <p>The image is {@code pgvector/pgvector:pg16}, the same one {@code docker-compose.yml}
 * runs, because {@code V1__init.sql} opens with {@code create extension vector} and a plain
 * {@code postgres} image cannot apply it. Testing against a different image than the one
 * that will be deployed is how a migration that only fails in production gets written.
 *
 * <p>Started once and never stopped: Testcontainers' own reaper removes it when the JVM
 * exits, and sharing it lets both Spring contexts in this module reuse a single database
 * rather than paying for a container each.
 */
final class SharedPostgres {

    private static final DockerImageName IMAGE =
            DockerImageName.parse("pgvector/pgvector:pg16").asCompatibleSubstituteFor("postgres");

    private static @Nullable PostgreSQLContainer<?> container;

    private SharedPostgres() {}

    /**
     * The running container. Called only from a {@code @DynamicPropertySource} method, which
     * runs after {@code @Testcontainers(disabledWithoutDocker = true)} has already decided
     * whether Docker is there — so a machine without Docker skips the test class loudly
     * instead of failing to start a container.
     */
    static synchronized PostgreSQLContainer<?> get() {
        PostgreSQLContainer<?> running = container;
        if (running == null) {
            running = new PostgreSQLContainer<>(IMAGE)
                    .withDatabaseName("judikat")
                    .withUsername("judikat")
                    .withPassword("judikat");
            running.start();
            container = running;
        }
        return running;
    }
}

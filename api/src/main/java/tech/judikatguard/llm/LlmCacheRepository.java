package tech.judikatguard.llm;

import java.util.Objects;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/**
 * The {@code llm_cache} table (V4). CLAUDE.md rule 7 and PLAN.md sections 8 and 10.
 *
 * <p>Every model call is cached. Configuration is fixed and temperature is 0 where the model
 * accepts it, so a hit is a faithful replay of a call that already passed the evidence-span
 * gate, not an approximation of one. That is what makes re-running a report free and makes
 * the demo reproducible offline.
 *
 * <p><b>The caller owns the key.</b> Each prompt's frontmatter documents the tuple its key is
 * built from ({@code (cited_ecli, claim_hash, prompt_version)} for the proposition check,
 * {@code (citing_ecli, cited_ecli, paragraph_idx, prompt_version)} for treatment
 * classification, {@code (from_version_id, to_version_id, prompt_version)} for provision
 * materiality, so that the many decisions relying on one provision share a single
 * judgement). This repository stores whatever string it is handed; inventing a key here would
 * put the caching policy in two places.
 */
public interface LlmCacheRepository {

    Optional<CachedCall> find(String cacheKey);

    /** Inserts, or replaces an entry that a caller has just found no longer valid. */
    void put(CachedCall call);

    /**
     * One row of {@code llm_cache}.
     *
     * @param cacheKey the tuple documented in the prompt's frontmatter, rendered as a string
     * @param promptVersion {@code PromptTemplate.version()}, never a hardcoded literal
     * @param model {@code LlmClient.model()}
     * @param request JSON: what was sent, enough to reproduce the call
     * @param response JSON: the validated reply, in the prompt's own output shape
     */
    record CachedCall(
            String cacheKey, String promptVersion, String model, String request, String response) {

        public CachedCall {
            Objects.requireNonNull(cacheKey, "cacheKey");
            Objects.requireNonNull(promptVersion, "promptVersion");
            Objects.requireNonNull(model, "model");
            Objects.requireNonNull(request, "request");
            Objects.requireNonNull(response, "response");
        }
    }

    /** The real one. Hand-written SQL over {@link JdbcClient}; no ORM (PLAN.md section 4). */
    @Repository
    final class Jdbc implements LlmCacheRepository {

        private static final String FIND_SQL = """
                select cache_key, prompt_version, model, request::text as request,
                       response::text as response
                  from llm_cache
                 where cache_key = :cacheKey
                """;

        // The jsonb casts are explicit because a bound parameter arrives as text. On conflict
        // we overwrite rather than ignore: the only caller that reaches this statement with an
        // existing key is one that just rejected the stored reply, and leaving the rejected
        // row in place would make it permanent.
        private static final String PUT_SQL = """
                insert into llm_cache (cache_key, prompt_version, model, request, response)
                values (:cacheKey, :promptVersion, :model,
                        cast(:request as jsonb), cast(:response as jsonb))
                on conflict (cache_key) do update
                   set prompt_version = excluded.prompt_version,
                       model          = excluded.model,
                       request        = excluded.request,
                       response       = excluded.response,
                       created_at     = now()
                """;

        private final JdbcClient jdbc;

        public Jdbc(JdbcClient jdbc) {
            this.jdbc = Objects.requireNonNull(jdbc, "jdbc");
        }

        @Override
        public Optional<CachedCall> find(String cacheKey) {
            return jdbc.sql(FIND_SQL)
                    .param("cacheKey", cacheKey)
                    .query((rs, rowNum) -> new CachedCall(
                            rs.getString("cache_key"),
                            rs.getString("prompt_version"),
                            rs.getString("model"),
                            rs.getString("request"),
                            rs.getString("response")))
                    .optional();
        }

        @Override
        public void put(CachedCall call) {
            jdbc.sql(PUT_SQL)
                    .param("cacheKey", call.cacheKey())
                    .param("promptVersion", call.promptVersion())
                    .param("model", call.model())
                    .param("request", call.request())
                    .param("response", call.response())
                    .update();
        }
    }
}

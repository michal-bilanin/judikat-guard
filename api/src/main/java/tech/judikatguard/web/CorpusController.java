package tech.judikatguard.web;

import java.util.Objects;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import tech.judikatguard.document.EvaluationContext;

/**
 * {@code GET /api/corpus}: the scope clause, on its own.
 *
 * <p>Exists so the UI can state what the corpus covers before anything has been checked.
 * "No adverse treatment found in 3142 decisions published through 15 August" is a claim a
 * lawyer can weigh; "valid" is not.
 */
@RestController
@RequestMapping("/api/corpus")
public class CorpusController {

    @GetMapping
    CorpusView corpus() {
        EvaluationContext context = EvaluationContext.current();
        return CorpusView.of(
                Objects.requireNonNull(context.corpus().get()), context.asOf());
    }
}

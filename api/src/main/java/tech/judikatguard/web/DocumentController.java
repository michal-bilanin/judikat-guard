package tech.judikatguard.web;

import jakarta.validation.Valid;
import java.util.Objects;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import tech.judikatguard.document.DocumentChecker;
import tech.judikatguard.document.DocumentReport;

/**
 * {@code POST /api/documents/check}: the endpoint the whole product hangs off.
 *
 * <p>Paste a legal document, get back every court decision and statutory provision it
 * relies on, each with a traffic light and traceable evidence, plus the references that
 * could not be placed at all.
 */
@RestController
@RequestMapping("/api/documents")
public class DocumentController {

    private final DocumentChecker checker;

    public DocumentController(DocumentChecker checker) {
        this.checker = Objects.requireNonNull(checker, "checker");
    }

    @PostMapping("/check")
    DocumentReport check(@Valid @RequestBody CheckRequest request) {
        return checker.check(request.text());
    }
}

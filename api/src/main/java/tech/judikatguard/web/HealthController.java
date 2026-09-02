package tech.judikatguard.web;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * {@code GET /api/health}. The M0 acceptance check, and deliberately trivial: it must not
 * touch the database, so that it answers the question "is the process up" and not "is
 * everything up".
 */
@RestController
@RequestMapping("/api/health")
public class HealthController {

    @GetMapping
    Health health() {
        return new Health("ok");
    }

    public record Health(String status) {}
}

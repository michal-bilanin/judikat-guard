package tech.judikatguard.web;

/**
 * The requested source is not in the corpus.
 *
 * <p>A 404 here means "this system has never seen it", which is a different statement from
 * "nothing adverse was found about it". Conflating the two would let a coverage gap read as
 * a clean bill of health, which is the failure mode PLAN.md section 16 puts on the
 * limitations slide.
 */
public final class NotFoundException extends RuntimeException {

    private static final long serialVersionUID = 1L;

    public NotFoundException(String czechDetail) {
        super(czechDetail);
    }
}

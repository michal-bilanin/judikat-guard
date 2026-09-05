"""Tier 1 of the router: treatment labels that need no model call at all.

PLAN.md section 8, decision D1: most treatment events in Czech decisions are procedurally
marked, and a marked event is a regex plus a metadata lookup. Two rules live here:

* a citation sitting inside a passage that reports what a *party* argued is not the court
  relying on the decision -> ``MENTIONED``,
* the citing decision's **výrok** annulling the cited decision *by its reference number*
  -> ``QUASHED``.

Both are pure functions over text plus metadata, so the whole tier is testable without a
database, without a model and without a network. The marker vocabulary is not defined here:
it is read from the ``[markers]`` tables of ``extract/patterns.toml``, the same file the
Java runtime reads, so the two runtimes cannot drift.

Precision is the design goal. A missed structural label costs one escalation to the next
tier; a *false* ``MENTIONED`` silently hides a real adverse treatment and a false
``QUASHED`` manufactures a red light, which PLAN.md D8 says is the one unacceptable
failure. Every rule below therefore refuses when it cannot verify its own premise.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pydantic import BaseModel

from jg.config import PATTERNS_TOML
from jg.models import PanelType, Route, TreatmentLabel, TreatmentResult

log = logging.getLogger(__name__)

#: ``treatment.prompt_version`` for a row this tier produced. The column is ``not null`` and
#: no prompt was involved, so it names the *ruleset* that produced the label instead. Same
#: convention as ``jg.extract.anaphora.PROMPT_VERSION``: a version string that a stored row
#: can be traced back to. Changing a rule below means adding ``.v2``, never editing ``.v1``.
RULESET_VERSION = "structural-router.v1"

#: PLAN.md section 8 fixes both numbers. 1.0 is honest for the výrok rule: an annulment
#: named in the operative part is a fact about the document, not an inference.
CONFIDENCE_PARTY_SUBMISSION = 0.95
CONFIDENCE_QUASHED = 1.0

#: ``decision_alias.alias_kind`` values usable as proof that a výrok names *this* decision.
#: ``journal_no`` is excluded on purpose: an R-číslo is a publication reference, and matching
#: one inside an operative part would be a coincidence rather than an identification.
QUASHING_ALIAS_KINDS = ("case_no", "ref_no", "ecli_variant")


def normalise_ws(text: str) -> str:
    """Collapse every run of whitespace to one ASCII space and strip the ends.

    The one normalisation used across this stage, for the same reason
    :func:`jg.extract.resolver.normalize_alias` exists: both sides of a comparison must be
    normalised the same way. ``str.split`` treats NBSP and the other unicode spaces as
    whitespace, which is what makes the evidence-span gate in :mod:`jg.classify.reasoning`
    survive a model that retypes a non-breaking space as a plain one.
    """
    return " ".join(text.split())


def fold(text: str) -> str:
    """:func:`normalise_ws` plus case folding. For marker and identifier matching only."""
    return normalise_ws(text).casefold()


# --------------------------------------------------------------------------- markers


@dataclass(frozen=True)
class MarkerSet:
    """The three ``[markers]`` lists from ``extract/patterns.toml``."""

    party_submission: tuple[str, ...]
    departure: tuple[str, ...]
    quashing: tuple[str, ...]


def load_markers(path: Path = PATTERNS_TOML) -> MarkerSet:
    """Read the ``[markers]`` tables straight out of the TOML. Uncached.

    Deliberately not routed through :func:`jg.extract.patterns.load_patterns`: this stage
    needs three string lists, not compiled regexes, and reading the shared file directly
    keeps the classifier importable while the extract stage is being written. The file
    stays the single source of truth either way.
    """
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    markers = raw.get("markers", {})

    def listed(name: str) -> tuple[str, ...]:
        values = markers.get(name, ())
        if not isinstance(values, list) or not values:
            raise ValueError(f"[markers].{name} is missing or empty in {path}")
        return tuple(str(value) for value in values)

    return MarkerSet(
        party_submission=listed("party_submission"),
        departure=listed("departure"),
        quashing=listed("quashing"),
    )


@cache
def marker_set() -> MarkerSet:
    """Module-level singleton. Parses the TOML once per process."""
    return load_markers()


# ------------------------------------------------------------------- text primitives


def sentences(text: str) -> list[tuple[int, str]]:
    """``(start_offset, sentence)`` pairs, offsets into ``text``.

    Borrows the extract stage's splitter, which already knows that Czech legal prose ends
    a third of its tokens in a full stop (``sp. zn.``, ``č. j.``, ``odst.``, ``12. 3.``)
    without ending a sentence. The import is deferred so this module still imports while
    ``jg.extract`` is mid-write; if the splitter is unavailable the paragraph is treated as
    one sentence, which widens a passage boundary rather than crashing the router.
    """
    try:
        from jg.extract.anaphora import split_sentences
    except ImportError:  # pragma: no cover - only when the extract stage is absent
        log.warning("jg.extract.anaphora unavailable: treating each paragraph as one sentence")
        return [(0, text)]
    return split_sentences(text)


def locate(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Every ``(start, end)`` where ``needle`` occurs in ``haystack``, whitespace-tolerantly.

    ``citation.raw_text`` is a verbatim slice of the paragraph, so a plain ``str.find``
    would usually do. It is matched token by token anyway because the same span can reach
    here re-typed: a non-breaking space in the stored paragraph, a normalised one in the
    citation, or the reverse. Matching is case-insensitive for the same reason.
    """
    tokens = needle.split()
    if not tokens:
        return []
    pattern = re.compile(r"\s+".join(re.escape(token) for token in tokens), re.IGNORECASE)
    return [match.span() for match in pattern.finditer(haystack)]


def find_marker(text: str, markers: Sequence[str]) -> str | None:
    """The first marker phrase present in ``text``, or None. Case- and whitespace-insensitive."""
    folded = fold(text)
    for marker in markers:
        if fold(marker) in folded:
            return marker
    return None


def citation_sentence(paragraph: str, citation_text: str) -> str | None:
    """The sentence of ``paragraph`` that contains ``citation_text``, verbatim.

    Returns None when the citation cannot be found, which is a refusal rather than a
    fallback: every span this stage stores has to be a real quote from a real paragraph.
    """
    spans = locate(paragraph, citation_text)
    if not spans:
        return None
    for start, sentence in sentences(paragraph):
        end = start + len(sentence)
        if any(span_start < end and span_end > start for span_start, span_end in spans):
            return sentence.strip()
    return None


def departure_span(text: str, markers: MarkerSet | None = None) -> str | None:
    """The sentence carrying a departure marker, or None. Tier 2's trigger. PLAN.md section 8.

    Lives here rather than in the router because it is the same kind of pure marker lookup
    as the two rules above; the router only decides what to do with the answer.
    """
    active = markers or marker_set()
    for _start, sentence in sentences(text):
        if find_marker(sentence, active.departure) is not None:
            return sentence.strip()
    return None


# ------------------------------------------------------------------------- the edge


def as_panel_type(value: object) -> PanelType:
    """``decision.panel_type`` -> :class:`PanelType`, tolerating an unexpected value."""
    try:
        return PanelType(str(value))
    except ValueError:
        log.warning("unknown decision.panel_type %r, treating as %s", value, PanelType.UNKNOWN)
        return PanelType.UNKNOWN


class CitationEdge(BaseModel):
    """One citation edge with everything all three tiers need. The extract -> classify seam.

    Assembled by :func:`jg.classify.router.load_edges` from ``citation`` joined to both
    ``decision`` rows. It is a *record*, not a query: every tier takes one of these and no
    tier reads the database, which is what lets the whole router be tested off-line.

    Two fields are worth reading twice. ``context`` is the citing paragraph plus up to two
    either side (PLAN.md section 8) and is the only text a model ever sees, so it is also
    the only text an ``evidence_span`` may be quoted from. ``verdict_text`` is the citing
    decision's **výrok** and is the *only* input to the ``QUASHED`` rule; ``decision`` has
    no column for it (see ``jg.normalize.store_decision``), so it arrives from the crawl
    cache and is frequently None.

    Lives in this module rather than in ``jg/models.py`` only because that file is a fixed
    cross-stage contract this task may not edit; it belongs there eventually.
    """

    citation_id: int
    citing_ecli: str
    citing_court: str
    citing_panel: PanelType = PanelType.UNKNOWN
    citing_date: dt.date | None = None
    cited_ecli: str
    cited_court: str | None = None
    cited_date: dt.date | None = None
    cited_ratio: str | None = None
    #: Aliases of the *cited* decision, normalised. The výrok must name one of these.
    cited_aliases: tuple[str, ...] = ()
    paragraph_idx: int | None = None
    #: The citation exactly as it appeared in the citing decision.
    raw_text: str
    #: The context window, oldest paragraph first. Empty when the paragraph is unknown.
    context: tuple[str, ...] = ()
    #: Position of the citing paragraph inside :attr:`context`.
    context_focus: int = 0
    #: The citing decision's výrok, from the crawl cache. None when it was not cached.
    verdict_text: str | None = None

    @property
    def citing_paragraph(self) -> str:
        """The paragraph the citation sits in, or ``""`` when the window is empty."""
        if not self.context or not 0 <= self.context_focus < len(self.context):
            return ""
        return self.context[self.context_focus]

    @property
    def context_text(self) -> str:
        """The context window as one string, paragraphs joined by a blank line."""
        return "\n\n".join(self.context)

    def identifiers(self) -> tuple[str, ...]:
        """Every string that identifies the *cited* decision, for the výrok rule."""
        seen: dict[str, None] = {}
        for value in (self.cited_ecli, *self.cited_aliases):
            folded = fold(value)
            if folded:
                seen.setdefault(folded, None)
        return tuple(seen)


# ------------------------------------------------------------------------ the rules


def party_submission_span(
    paragraph: str, citation_text: str, markers: MarkerSet | None = None
) -> str | None:
    """The party-submission sentence the citation sits in, or None.

    The passage boundary is one sentence: the citation must appear in the *same* sentence as
    the marker. Czech decisions do run a party's argument across several sentences, so this
    under-detects. That is the intended direction. The alternative boundary — marker to end
    of paragraph — swallows the court's own answer, which very often follows in the next
    sentence ("Stěžovatel odkazuje na ... . Nejvyšší správní soud však vychází z ..."), and
    would relabel the court's reliance as a party's mention. A miss costs one model call; a
    false ``MENTIONED`` hides an adverse treatment for good.
    """
    active = markers or marker_set()
    spans = locate(paragraph, citation_text)
    if not spans:
        return None
    for start, sentence in sentences(paragraph):
        end = start + len(sentence)
        if find_marker(sentence, active.party_submission) is None:
            continue
        if any(span_start < end and span_end > start for span_start, span_end in spans):
            return sentence.strip()
    return None


def quashed_span(
    verdict_text: str | None,
    identifiers: Sequence[str],
    markers: MarkerSet | None = None,
) -> str | None:
    """The výrok sentence that annuls *this* cited decision, or None.

    Three conditions, all required, all checked against the operative part only:

    1. ``verdict_text`` is present — no výrok, no rule,
    2. a quashing marker (*se ruší*, *zrušuje se*, ...) occurs in a sentence of it,
    3. **the same sentence** names the cited decision by one of its identifiers.

    Condition 3 with the same-sentence restriction is the whole point. A výrok reading
    "Ústavní stížnost proti rozsudku ..., č. j. ..., se odmítá. Rozhodnutí správního orgánu
    se ruší." contains both a marker and the cited reference and annuls something else
    entirely. Inferring ``QUASHED`` from *zrušuje* alone would mark the cited decision as
    dead on the strength of a lower-court decision being annulled, which is the common case.
    """
    if not verdict_text or not verdict_text.strip():
        return None
    active = markers or marker_set()
    wanted = [fold(value) for value in identifiers if fold(value)]
    if not wanted:
        return None

    for _start, sentence in sentences(verdict_text):
        if find_marker(sentence, active.quashing) is None:
            continue
        folded = fold(sentence)
        if not any(identifier in folded for identifier in wanted):
            continue
        return sentence.strip()
    return None


def annulment_is_chronological(edge: CitationEdge) -> bool:
    """Could the citing decision actually have annulled the cited one?

    A court cannot annul a decision that did not exist yet, so the cited decision must
    predate the citing one. Cheap, absolute, and it catches a whole class of resolution
    error that the výrok rule cannot see: when a citation is matched to the wrong decision
    of the same case, the wrong one is very often the *later* decision issued on remand
    after the annulment, which inverts the dates.

    A missing date means the check cannot be made, and an unverifiable annulment is refused
    rather than assumed. That costs recall on rows with no ``decided_on``; per D8 a false
    red costs far more. Both dates come from ``decision.decided_on``, which is ``not null``,
    so in practice this only bites when an edge was assembled without them.
    """
    if edge.citing_date is None or edge.cited_date is None:
        return False
    return edge.cited_date < edge.citing_date


def classify_structural(
    edge: CitationEdge, markers: MarkerSet | None = None
) -> TreatmentResult | None:
    """Tier 1 in full: a finished :class:`TreatmentResult`, or None to fall through.

    The výrok rule is tried first. It is the stronger signal — a fact about the operative
    part rather than a reading of the reasoning — and it outranks the party-submission rule
    in the one case where both fire: a decision whose výrok annuls the very decision a party
    had cited is quashed, whoever brought it up.
    """
    active = markers or marker_set()

    span = quashed_span(edge.verdict_text, edge.identifiers(), active)
    if span is not None and annulment_is_chronological(edge):
        return _result(edge, TreatmentLabel.QUASHED, CONFIDENCE_QUASHED, span)

    span = party_submission_span(edge.citing_paragraph, edge.raw_text, active)
    if span is not None:
        return _result(edge, TreatmentLabel.MENTIONED, CONFIDENCE_PARTY_SUBMISSION, span)

    return None


def _result(
    edge: CitationEdge, label: TreatmentLabel, confidence: float, span: str
) -> TreatmentResult:
    return TreatmentResult(
        citation_id=edge.citation_id,
        label=label,
        confidence=confidence,
        evidence_span=span,
        route=Route.STRUCTURAL,
        model=None,
        prompt_version=RULESET_VERSION,
    )

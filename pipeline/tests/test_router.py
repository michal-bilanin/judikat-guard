"""The router: which tier an edge lands in, and what that costs.

No network, no database, no model. :func:`jg.classify.router.route` is a pure decision
function by design — that is what makes the statistics mode possible and what makes the
20k-edges-to-1500-calls projection in PLAN.md section 8 something you can measure instead
of assert.

Identifiers: the cited decision is the ECLI quoted in PLAN.md section 11; the citing
decision uses a ``TEST-`` prefix that cannot resolve (CLAUDE.md rule 1).
"""

from __future__ import annotations

import datetime as dt

import pytest

from jg.classify.router import (
    TRIAGE_CONFIDENCE,
    TRIAGE_RULESET_VERSION,
    TRIAGE_THRESHOLD,
    CitationEdge,
    Escalation,
    EscalationReason,
    RouterStats,
    TriageVerdict,
    context_window,
    default_triage,
    dry_run,
    route,
    route_all,
    stats_table,
)
from jg.models import PanelType, Route, TreatmentLabel

REF_NO = "č. j. 6 Ads 45/2014-32"
CASE_NO = "6 Ads 45/2014-32"
CITED_ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32"
CITING_ECLI = "TEST-citing-decision"

#: The court relying on the cited decision in its own voice: nothing structural fires.
ORDINARY = (
    "Nejvyšší správní soud vychází z rozsudku ze dne 12. 3. 2015, "
    f"{REF_NO}, který na posuzovanou věc dopadá přímo."
)
BEFORE = "K první kasační námitce se soud vyjádřil již v bodě 21."
AFTER = "Z těchto důvodů soud kasační stížnost zamítl."

PARTY = f"Stěžovatel odkazuje na rozsudek ze dne 12. 3. 2015, {REF_NO}."
VERDICT_QUASHING_CITED = (
    f"Rozsudek Nejvyššího správního soudu ze dne 12. 3. 2015, {REF_NO}, se ruší."
)

#: A departure marker from ``extract/patterns.toml``, in a *neighbouring* paragraph.
DEPARTURE_NEIGHBOUR = (
    "Rozšířený senát dospěl k závěru, že na dosavadním výkladu nelze setrvat."
)


def edge(**overrides: object) -> CitationEdge:
    fields: dict[str, object] = {
        "citation_id": 1,
        "citing_ecli": CITING_ECLI,
        "citing_court": "NSS",
        "citing_panel": PanelType.PANEL,
        "citing_date": dt.date(2026, 9, 1),
        "cited_ecli": CITED_ECLI,
        "cited_court": "NSS",
        "cited_date": dt.date(2015, 3, 12),
        "cited_aliases": (CASE_NO.casefold(),),
        "paragraph_idx": 34,
        "raw_text": REF_NO,
        "context": (BEFORE, ORDINARY, AFTER),
        "context_focus": 1,
    }
    fields.update(overrides)
    return CitationEdge(**fields)  # type: ignore[arg-type]


def fixed_triage(confidence: float, span: str | None = None):
    """A triage stub. The cheap tier is injectable precisely so a test can pin it."""

    def triage(subject: CitationEdge) -> TriageVerdict:
        return TriageVerdict(
            TreatmentLabel.FOLLOWED, confidence, span if span is not None else ORDINARY
        )

    return triage


# --- tier 1 wins ------------------------------------------------------------------------


def test_structural_label_short_circuits_the_router():
    routing = route(edge(context=(BEFORE, PARTY, AFTER)))
    assert not isinstance(routing, Escalation)
    assert routing.label is TreatmentLabel.MENTIONED
    assert routing.route is Route.STRUCTURAL


def test_structural_label_outranks_the_forced_panel_escalation():
    """An extended senát reporting what a party cited is still only reporting it."""
    routing = route(
        edge(context=(BEFORE, PARTY, AFTER), citing_panel=PanelType.EXTENDED)
    )
    assert not isinstance(routing, Escalation)
    assert routing.label is TreatmentLabel.MENTIONED


def test_verdict_annulment_outranks_the_forced_panel_escalation():
    routing = route(edge(citing_panel=PanelType.PLENARY, verdict_text=VERDICT_QUASHING_CITED))
    assert not isinstance(routing, Escalation)
    assert routing.label is TreatmentLabel.QUASHED
    assert routing.confidence == 1.0


# --- tier 2: forced escalation ----------------------------------------------------------


@pytest.mark.parametrize(
    "panel", [PanelType.EXTENDED, PanelType.GRAND, PanelType.PLENARY]
)
def test_departure_capable_panels_are_always_escalated(panel):
    routing = route(edge(citing_panel=panel))
    assert isinstance(routing, Escalation)
    assert routing.reason is EscalationReason.PANEL_TYPE
    assert str(routing.reason) == "panel_type"
    #: Escalated before triage ran, so nothing was guessed.
    assert routing.triaged is None


@pytest.mark.parametrize("panel", [PanelType.PANEL, PanelType.UNKNOWN])
def test_ordinary_panels_are_not_escalated_on_panel_type(panel):
    routing = route(edge(citing_panel=panel))
    assert not isinstance(routing, Escalation)


def test_a_departure_marker_in_a_surrounding_paragraph_escalates():
    """PLAN.md says *surrounding* text, so the window either side counts, not just the hit."""
    routing = route(edge(context=(DEPARTURE_NEIGHBOUR, ORDINARY, AFTER)))
    assert isinstance(routing, Escalation)
    assert routing.reason is EscalationReason.DEPARTURE_MARKER


@pytest.mark.parametrize(
    "marker_sentence",
    [
        "Osmý senát se hodlá odchýlit se od dosavadního výkladu.",
        "Tento výklad byl překonán pozdější judikaturou.",
        "Soud koriguje dosavadní výklad.",
        "Věc postoupil rozšířenému senátu.",
    ],
)
def test_each_departure_marker_form_escalates(marker_sentence):
    routing = route(edge(context=(BEFORE, ORDINARY, marker_sentence)))
    assert isinstance(routing, Escalation)
    assert routing.reason is EscalationReason.DEPARTURE_MARKER


# --- tier 3: triage and the confidence threshold ----------------------------------------


def test_triage_at_the_threshold_is_kept():
    routing = route(edge(), triage=fixed_triage(TRIAGE_THRESHOLD))
    assert not isinstance(routing, Escalation)
    assert routing.route is Route.TRIAGE
    assert routing.confidence == TRIAGE_THRESHOLD
    assert routing.prompt_version == TRIAGE_RULESET_VERSION
    assert routing.model is None


def test_triage_just_below_the_threshold_escalates():
    routing = route(edge(), triage=fixed_triage(TRIAGE_THRESHOLD - 0.01))
    assert isinstance(routing, Escalation)
    assert routing.reason is EscalationReason.LOW_CONFIDENCE
    assert routing.triaged is not None
    assert routing.triaged.confidence == pytest.approx(TRIAGE_THRESHOLD - 0.01)


def test_triage_escalates_when_it_cannot_quote_the_context():
    """CLAUDE.md rule 3 is not tier-specific: an unquotable label is not stored."""
    invented = "Soud tento závěr výslovně odmítl."
    routing = route(edge(), triage=fixed_triage(0.99, invented))
    assert isinstance(routing, Escalation)
    assert routing.reason is EscalationReason.LOW_CONFIDENCE


def test_default_triage_labels_a_plain_supporting_citation():
    verdict = default_triage(edge())
    assert verdict.label is TreatmentLabel.FOLLOWED
    assert verdict.confidence == TRIAGE_CONFIDENCE
    assert verdict.confidence >= TRIAGE_THRESHOLD
    assert verdict.evidence_span in ORDINARY


def test_default_triage_refuses_without_a_locatable_citation():
    verdict = default_triage(edge(context=(), context_focus=0))
    assert verdict.confidence == 0.0
    assert verdict.evidence_span == ""


def test_an_edge_with_no_context_escalates():
    routing = route(edge(context=(), context_focus=0, paragraph_idx=None))
    assert isinstance(routing, Escalation)
    assert routing.reason is EscalationReason.LOW_CONFIDENCE


# --- the statistics mode ----------------------------------------------------------------


def test_dry_run_counts_every_tier_without_calling_a_model():
    edges = [
        edge(citation_id=1, context=(BEFORE, PARTY, AFTER)),
        edge(citation_id=2, verdict_text=VERDICT_QUASHING_CITED),
        edge(citation_id=3, citing_panel=PanelType.EXTENDED),
        edge(citation_id=4, context=(DEPARTURE_NEIGHBOUR, ORDINARY, AFTER)),
        edge(citation_id=5, context=(), paragraph_idx=None),
        edge(citation_id=6),
    ]
    stats = dry_run(edges)

    assert stats.total == 6
    assert stats.structural == {"MENTIONED": 1, "QUASHED": 1}
    assert stats.triage == {"FOLLOWED": 1}
    assert stats.escalated == {
        "panel_type": 1,
        "departure_marker": 1,
        "low_confidence": 1,
    }
    assert stats.reasoning_calls == 3
    assert stats.reasoning_share == pytest.approx(0.5)
    #: A dry run makes no calls, so nothing was labelled by the model.
    assert stats.reasoning == {}
    assert stats.unclassified_share == 0.0


def test_route_all_returns_one_decision_per_edge():
    edges = [edge(citation_id=1), edge(citation_id=2, citing_panel=PanelType.GRAND)]
    routings, stats = route_all(edges)
    assert len(routings) == 2
    assert stats.total == 2
    assert isinstance(routings[1], Escalation)


def test_unclassified_share_is_measured_against_reasoning_rows_only():
    stats = RouterStats()
    stats.escalated["panel_type"] = 2
    stats.reasoning["DEPARTED"] = 1
    stats.reasoning["UNCLASSIFIED"] = 1
    assert stats.unclassified_share == pytest.approx(0.5)


def test_stats_table_renders_in_czech():
    table = stats_table(dry_run([edge()]))
    assert table.title.startswith("Směrování")
    assert [column.header for column in table.columns] == ["Vrstva", "Hran", "Podíl"]


# --- the context window ------------------------------------------------------------------


def test_context_window_takes_two_paragraphs_either_side():
    paragraphs = [(idx, f"odstavec {idx}") for idx in range(1, 8)]
    window, focus = context_window(paragraphs, 4)
    assert window == tuple(f"odstavec {idx}" for idx in range(2, 7))
    assert window[focus] == "odstavec 4"


def test_context_window_is_clipped_at_the_start_of_the_document():
    paragraphs = [(idx, f"odstavec {idx}") for idx in range(1, 8)]
    window, focus = context_window(paragraphs, 1)
    assert window == ("odstavec 1", "odstavec 2", "odstavec 3")
    assert focus == 0


def test_context_window_walks_real_paragraphs_across_an_index_gap():
    """``decision_paragraph.idx`` has gaps where blank paragraphs were dropped."""
    paragraphs = [(1, "a"), (2, "b"), (7, "c"), (8, "d"), (9, "e")]
    window, focus = context_window(paragraphs, 7)
    assert window == ("a", "b", "c", "d", "e")
    assert window[focus] == "c"


def test_context_window_is_empty_for_an_unknown_paragraph():
    assert context_window([(1, "a")], None) == ((), 0)
    assert context_window([(1, "a")], 5) == ((), 0)
    assert context_window([], 1) == ((), 0)

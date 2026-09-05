"""Tier 1 of the router: the two rules that need no model call.

Every identifier in this file is one that appears literally in PLAN.md or in a comment in
``extract/patterns.toml`` — ``6 Ads 45/2014-32``, its ECLI, ``II. ÚS 2379/08`` — or carries
the ``TEST-`` prefix that cannot resolve against any corpus. Nothing here is invented
(CLAUDE.md rule 1). The Czech prose around them is written for the test; the *identifiers*
are the part that must be real.

The negative cases are the point of this file. Both rules fire on a marker plus a position,
and it is the position that keeps them honest: a party-submission marker in the paragraph
does not make the court's own citation a mention, and *se ruší* in a výrok does not mean the
cited decision is the thing being annulled.
"""

from __future__ import annotations

import datetime as dt

import pytest

from jg.classify.structural import (
    CONFIDENCE_PARTY_SUBMISSION,
    CONFIDENCE_QUASHED,
    RULESET_VERSION,
    CitationEdge,
    annulment_is_chronological,
    as_panel_type,
    citation_sentence,
    classify_structural,
    departure_span,
    find_marker,
    load_markers,
    locate,
    marker_set,
    normalise_ws,
    party_submission_span,
    quashed_span,
)
from jg.models import PanelType, Route, TreatmentLabel

# --- identifiers, all quoted from PLAN.md / extract/patterns.toml -------------------------

REF_NO = "č. j. 6 Ads 45/2014-32"
CASE_NO = "6 Ads 45/2014-32"
CITED_ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32"
OTHER_CASE_NO = "II. ÚS 2379/08"
CITING_ECLI = "TEST-citing-decision"

# --- text fixtures -----------------------------------------------------------------------

PARTY_PARAGRAPH = (
    "Stěžovatel odkazuje na rozsudek Nejvyššího správního soudu ze dne 12. 3. 2015, "
    f"{REF_NO}, z něhož podle jeho názoru vyplývá opačný závěr."
)

#: The marker is present, but in a *different* sentence from the citation: the court is
#: answering the stěžovatel, not reporting him. This must not become MENTIONED.
COURT_VOICE_PARAGRAPH = (
    "Stěžovatel odkazuje na ustálenou judikaturu tohoto soudu. "
    "Nejvyšší správní soud však vychází z rozsudku ze dne 12. 3. 2015, "
    f"{REF_NO}, který na věc dopadá přímo."
)

VERDICT_QUASHING_CITED = (
    f"Rozsudek Nejvyššího správního soudu ze dne 12. 3. 2015, {REF_NO}, se ruší."
)

#: Both signals present, neither about the same decision: the cited judgment is what the
#: complaint was aimed at, and what is annulled is the administrative authority's decision.
VERDICT_QUASHING_SOMETHING_ELSE = (
    f"Ústavní stížnost proti rozsudku ze dne 12. 3. 2015, {REF_NO}, se odmítá. "
    "Rozhodnutí správního orgánu se ruší."
)


def edge(**overrides: object) -> CitationEdge:
    """A minimal edge; every field a test cares about is overridable."""
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
        "context": (PARTY_PARAGRAPH,),
        "context_focus": 0,
    }
    fields.update(overrides)
    return CitationEdge(**fields)  # type: ignore[arg-type]


# --- markers come from the shared TOML ---------------------------------------------------


def test_markers_are_read_from_the_shared_patterns_file():
    markers = load_markers()
    assert "stěžovatel odkazuje" in markers.party_submission
    assert "dovolatel poukazuje" in markers.party_submission
    assert "postoupil rozšířenému senátu" in markers.departure
    assert "se ruší" in markers.quashing


def test_marker_set_is_a_singleton():
    assert marker_set() is marker_set()


def test_find_marker_ignores_case_and_whitespace():
    assert find_marker("Podle\n Stěžovatele je závěr nesprávný.", ("podle stěžovatele",))
    assert find_marker("Soud vychází z ustálené judikatury.", ("podle stěžovatele",)) is None


# --- text primitives ---------------------------------------------------------------------


def test_normalise_ws_collapses_nbsp():
    assert normalise_ws("č. j. 6 Ads  45/2014-32 ") == "č. j. 6 Ads 45/2014-32"


def test_locate_matches_across_a_non_breaking_space():
    paragraph = f"Soud odkazuje na {REF_NO}."
    retyped = REF_NO.replace(" ", "\xa0")
    (start, end), = locate(paragraph, retyped)
    assert paragraph[start:end] == REF_NO


def test_locate_returns_nothing_for_an_absent_citation():
    assert locate(PARTY_PARAGRAPH, OTHER_CASE_NO) == []


def test_citation_sentence_quotes_the_sentence_the_citation_is_in():
    sentence = citation_sentence(COURT_VOICE_PARAGRAPH, REF_NO)
    assert sentence is not None
    assert sentence.startswith("Nejvyšší správní soud však")
    assert sentence in COURT_VOICE_PARAGRAPH


def test_citation_sentence_refuses_when_the_citation_is_not_there():
    assert citation_sentence(PARTY_PARAGRAPH, OTHER_CASE_NO) is None


# --- party-submission MENTIONED ----------------------------------------------------------


def test_party_submission_span_is_the_marker_sentence():
    span = party_submission_span(PARTY_PARAGRAPH, REF_NO)
    assert span == PARTY_PARAGRAPH
    assert span in PARTY_PARAGRAPH


@pytest.mark.parametrize(
    "marker_sentence",
    [
        "Žalobce namítá, že závěr je nesprávný, a odkazuje na {ref}.",
        "Podle stěžovatele z {ref} vyplývá opačný závěr.",
        "Dovolatel poukazuje na {ref}.",
    ],
)
def test_party_submission_span_fires_for_each_marker_form(marker_sentence):
    paragraph = marker_sentence.format(ref=REF_NO)
    assert party_submission_span(paragraph, REF_NO) == paragraph


def test_party_submission_span_ignores_a_citation_outside_the_passage():
    """The negative case: marker present, citation in the court's own next sentence."""
    assert party_submission_span(COURT_VOICE_PARAGRAPH, REF_NO) is None


def test_party_submission_span_needs_a_marker():
    paragraph = f"Nejvyšší správní soud vychází z rozsudku {REF_NO}."
    assert party_submission_span(paragraph, REF_NO) is None


def test_party_submission_span_refuses_when_the_citation_is_not_in_the_paragraph():
    assert party_submission_span(PARTY_PARAGRAPH, OTHER_CASE_NO) is None


# --- výrok-based QUASHED -----------------------------------------------------------------


def test_quashed_span_is_the_verdict_sentence_naming_the_cited_decision():
    span = quashed_span(VERDICT_QUASHING_CITED, (CASE_NO,))
    assert span == VERDICT_QUASHING_CITED


def test_quashed_span_matches_on_the_ecli_too():
    verdict = f"Rozsudek {CITED_ECLI} se zrušuje."
    assert quashed_span(verdict, (CITED_ECLI,)) == verdict


def test_quashed_span_matches_across_a_non_breaking_space():
    verdict = VERDICT_QUASHING_CITED.replace("6 Ads", "6\xa0Ads")
    assert quashed_span(verdict, (CASE_NO,)) == verdict


def test_quashed_span_ignores_a_verdict_annulling_a_different_decision():
    """The negative case: *se ruší* is about something else entirely."""
    assert quashed_span(VERDICT_QUASHING_SOMETHING_ELSE, (CASE_NO,)) is None


def test_quashed_span_ignores_a_verdict_that_does_not_name_the_cited_decision():
    assert quashed_span(VERDICT_QUASHING_CITED, (OTHER_CASE_NO,)) is None


def test_quashed_span_needs_a_quashing_marker():
    verdict = f"Kasační stížnost proti rozsudku ze dne 12. 3. 2015, {REF_NO}, se zamítá."
    assert quashed_span(verdict, (CASE_NO,)) is None


@pytest.mark.parametrize("verdict", [None, "", "   "])
def test_quashed_span_needs_a_verdict(verdict):
    assert quashed_span(verdict, (CASE_NO,)) is None


def test_quashed_span_needs_at_least_one_identifier():
    assert quashed_span(VERDICT_QUASHING_CITED, ()) is None
    assert quashed_span(VERDICT_QUASHING_CITED, ("", "   ")) is None


# --- the chronology guard on QUASHED ------------------------------------------------------


def test_quashed_is_refused_when_the_cited_decision_postdates_the_annulment():
    """A court cannot annul a decision that did not exist yet.

    Regression, and it was a real false red: a citation to an annulled decision resolved to
    the *later* decision the same court issued on remand, so the výrok rule fired against a
    decision decided after the annulment. Four of nine QUASHED rows in the first real run
    were this shape.
    """
    candidate = edge(
        citing_date=dt.date(2018, 10, 25),
        cited_date=dt.date(2020, 5, 28),
        verdict_text=VERDICT_QUASHING_CITED,
    )
    assert annulment_is_chronological(candidate) is False
    # The výrok rule is refused; the edge still falls through to the remaining tier-1 rules.
    result = classify_structural(candidate)
    assert result is None or result.label is not TreatmentLabel.QUASHED


def test_quashed_is_allowed_when_the_cited_decision_predates_the_annulment():
    candidate = edge(
        citing_date=dt.date(2021, 8, 24),
        cited_date=dt.date(2020, 10, 29),
        verdict_text=VERDICT_QUASHING_CITED,
    )
    assert annulment_is_chronological(candidate) is True
    result = classify_structural(candidate)
    assert result is not None
    assert result.label is TreatmentLabel.QUASHED
    assert result.confidence == CONFIDENCE_QUASHED


@pytest.mark.parametrize(
    ("citing_date", "cited_date"),
    [(None, dt.date(2020, 1, 1)), (dt.date(2021, 1, 1), None), (None, None)],
)
def test_quashed_is_refused_when_a_date_is_missing(citing_date, cited_date):
    """An unverifiable annulment is refused, not assumed. D8: a false red is unacceptable."""
    candidate = edge(
        citing_date=citing_date, cited_date=cited_date, verdict_text=VERDICT_QUASHING_CITED
    )
    assert annulment_is_chronological(candidate) is False
    result = classify_structural(candidate)
    assert result is None or result.label is not TreatmentLabel.QUASHED


def test_same_day_annulment_is_refused():
    """Strictly before, not on or before: a decision annulled the day it issued is not a
    thing, and treating equality as valid would readmit the self-citation case."""
    same = dt.date(2021, 8, 24)
    assert annulment_is_chronological(
        edge(citing_date=same, cited_date=same, verdict_text=VERDICT_QUASHING_CITED)
    ) is False


# --- departure markers, tier 2's trigger -------------------------------------------------


def test_departure_span_finds_the_marker_sentence():
    text = (
        "Osmý senát dospěl k závěru, že se nelze setrvat na dosavadním výkladu. "
        "Věc proto postupuje rozšířenému senátu."
    )
    assert departure_span(text) == (
        "Osmý senát dospěl k závěru, že se nelze setrvat na dosavadním výkladu."
    )


def test_departure_span_is_none_without_a_marker():
    assert departure_span(PARTY_PARAGRAPH) is None


# --- the tier as a whole -----------------------------------------------------------------


def test_classify_structural_labels_a_party_submission_mentioned():
    result = classify_structural(edge())
    assert result is not None
    assert result.label is TreatmentLabel.MENTIONED
    assert result.confidence == CONFIDENCE_PARTY_SUBMISSION
    assert result.route is Route.STRUCTURAL
    assert result.model is None
    assert result.prompt_version == RULESET_VERSION
    assert result.evidence_span in PARTY_PARAGRAPH


def test_classify_structural_labels_a_verdict_annulment_quashed():
    result = classify_structural(
        edge(context=(COURT_VOICE_PARAGRAPH,), verdict_text=VERDICT_QUASHING_CITED)
    )
    assert result is not None
    assert result.label is TreatmentLabel.QUASHED
    assert result.confidence == CONFIDENCE_QUASHED
    assert result.route is Route.STRUCTURAL
    assert result.evidence_span == VERDICT_QUASHING_CITED


def test_quashed_outranks_party_submission():
    """A decision whose výrok annuls the very judgment a party cited is still quashed."""
    result = classify_structural(edge(verdict_text=VERDICT_QUASHING_CITED))
    assert result is not None
    assert result.label is TreatmentLabel.QUASHED


def test_classify_structural_falls_through_for_an_ordinary_citation():
    ordinary = f"Nejvyšší správní soud vychází z rozsudku ze dne 12. 3. 2015, {REF_NO}."
    assert classify_structural(edge(context=(ordinary,))) is None


def test_classify_structural_falls_through_when_the_verdict_is_not_cached():
    assert classify_structural(edge(context=(COURT_VOICE_PARAGRAPH,), verdict_text=None)) is None


# --- the edge record ---------------------------------------------------------------------


def test_edge_exposes_its_focus_paragraph_and_joined_context():
    window = ("před", PARTY_PARAGRAPH, "po")
    subject = edge(context=window, context_focus=1)
    assert subject.citing_paragraph == PARTY_PARAGRAPH
    assert subject.context_text == "před\n\n" + PARTY_PARAGRAPH + "\n\npo"


def test_edge_with_an_empty_window_has_no_focus_paragraph():
    assert edge(context=(), context_focus=0).citing_paragraph == ""


def test_edge_identifiers_fold_and_deduplicate():
    identifiers = edge(cited_aliases=(CASE_NO, CASE_NO.casefold())).identifiers()
    assert identifiers == (CITED_ECLI.casefold(), CASE_NO.casefold())


def test_as_panel_type_tolerates_an_unknown_value():
    assert as_panel_type("extended") is PanelType.EXTENDED
    assert as_panel_type("senát velký") is PanelType.UNKNOWN

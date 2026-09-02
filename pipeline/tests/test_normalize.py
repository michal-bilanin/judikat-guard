"""Unit tests for the crawl -> rows mapping. No database.

The SQL in ``jg.normalize`` is exercised against a real Postgres by the Java-side
integration tests; what is worth unit-testing here is the pure row-shaping, because that is
where an alias can silently go missing or a paragraph index can slip.
"""

from __future__ import annotations

import datetime as dt

import pytest

from jg.extract.resolver import normalize_alias
from jg.models import AliasKind, PanelType, RawDecision
from jg.normalize import alias_rows, paragraph_rows

ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32"
CASE_NO = "6 Ads 45/2014"
REF_NO = "6 Ads 45/2014-32"
JOURNAL_NO = "R 12/2015"
SOURCE_URL = "https://www.nssoud.cz/"


def decision(**overrides: object) -> RawDecision:
    fields: dict[str, object] = {
        "ecli": ECLI,
        "court_code": "NSS",
        "panel_type": PanelType.PANEL,
        "decided_on": dt.date(2015, 3, 12),
        "case_no": CASE_NO,
        "ref_no": REF_NO,
        "journal_no": JOURNAL_NO,
        "source_url": SOURCE_URL,
        "fetched_at": dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC),
        "paragraphs": ["První odstavec.", "Druhý odstavec."],
    }
    fields.update(overrides)
    return RawDecision(**fields)


# --- aliases ------------------------------------------------------------------------------


def test_alias_rows_covers_every_identifier_the_decision_carries():
    rows = alias_rows(decision())
    by_alias = {alias: (kind, ecli) for alias, kind, ecli in rows}
    assert by_alias == {
        normalize_alias(ECLI): (str(AliasKind.ECLI_VARIANT), ECLI),
        normalize_alias(CASE_NO): (str(AliasKind.CASE_NO), ECLI),
        normalize_alias(REF_NO): (str(AliasKind.REF_NO), ECLI),
        normalize_alias(JOURNAL_NO): (str(AliasKind.JOURNAL_NO), ECLI),
    }


def test_alias_rows_are_written_normalised_so_the_resolver_matches_them():
    rows = alias_rows(decision(case_no=f"  {CASE_NO}  "))
    aliases = [alias for alias, _, _ in rows]
    assert normalize_alias(CASE_NO) in aliases
    assert all(alias == normalize_alias(alias) for alias in aliases)


def test_a_missing_journal_number_produces_no_row():
    rows = alias_rows(decision(journal_no=None))
    assert len(rows) == 3
    assert all(kind != str(AliasKind.JOURNAL_NO) for _, kind, _ in rows)


def test_an_empty_journal_number_produces_no_row():
    rows = alias_rows(decision(journal_no=""))
    assert len(rows) == 3


def test_identical_identifiers_collapse_to_one_alias_row():
    # decision_alias.alias is the primary key, so two identifiers normalising to the same
    # string must yield one row rather than a conflicting pair.
    rows = alias_rows(decision(ref_no=CASE_NO))
    aliases = [alias for alias, _, _ in rows]
    assert len(aliases) == len(set(aliases)) == 3


def test_every_alias_row_points_at_the_decisions_own_ecli():
    assert all(ecli == ECLI for _, _, ecli in alias_rows(decision()))


# --- paragraphs ---------------------------------------------------------------------------


def test_paragraph_index_is_one_based_and_follows_the_raw_order():
    rows = paragraph_rows(decision())
    assert rows == [
        (ECLI, 1, "První odstavec."),
        (ECLI, 2, "Druhý odstavec."),
    ]


def test_blank_paragraphs_are_skipped_without_renumbering_the_rest():
    # Renumbering would invalidate every stored citation.paragraph_idx, so a blank leaves a
    # gap rather than shifting its successors.
    rows = paragraph_rows(decision(paragraphs=["První.", "   ", "Třetí."]))
    assert [idx for _, idx, _ in rows] == [1, 3]


def test_a_decision_with_no_text_yields_no_paragraph_rows():
    assert paragraph_rows(decision(paragraphs=[])) == []


# --- the seam itself ----------------------------------------------------------------------


def test_raw_decision_defaults_stay_as_the_contract_declares_them():
    minimal = RawDecision(
        ecli=ECLI,
        court_code="NSS",
        decided_on=dt.date(2015, 3, 12),
        source_url=SOURCE_URL,
        fetched_at=dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC),
    )
    assert minimal.panel_type is PanelType.UNKNOWN
    assert minimal.paragraphs == []
    assert alias_rows(minimal) == [(normalize_alias(ECLI), str(AliasKind.ECLI_VARIANT), ECLI)]


@pytest.mark.parametrize("panel", list(PanelType))
def test_every_panel_type_survives_the_round_trip(panel: PanelType):
    assert str(decision(panel_type=panel).panel_type) == panel.value

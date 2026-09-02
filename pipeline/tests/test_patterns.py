"""Unit tests for the rules pass.

Every citation string here appears literally in PLAN.md or in a comment in
``extract/patterns.toml``. Nothing in this file is an invented identifier (CLAUDE.md rule 1).
"""

from __future__ import annotations

import pytest

from jg.extract.patterns import (
    CompiledPattern,
    drop_contained,
    find_references,
    load_patterns,
    pattern_set,
)
from jg.models import Reference, ReferenceKind

# --- real citation strings, all quoted from PLAN.md / extract/patterns.toml ---------------

REF_NO_IN_SENTENCE = "rozsudek NSS ze dne 12. 3. 2015, č. j. 6 Ads 45/2014-32"
REF_NO_NO_SHEET = "č. j. 6 Ads 45/2014"
CASE_NO_GEN = "sp. zn. 6 Ads 45/2014"
CASE_NO_US = "sp. zn. II. ÚS 2379/08"
# Both plenary numbers are real, read out of the NALUS page cached under data/raw/US.
CASE_NO_US_PLENARY = "sp. zn. Pl. ÚS 29/98"
CASE_NO_US_PLENARY_2 = "sp. zn. Pl. ÚS 36/93"
CASE_NO_US_NO_DESIGNATOR = "sp. zn. ÚS 1/09"
ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32"
JOURNAL = "R 12/2015"
PROVISION_WITH_ACT = "§ 2000 odst. 1 zákona č. 89/2012 Sb."
PROVISION_NO_ACT = "§ 2000 odst. 1"
PROVISION_BARE = "§17 s.ř.s."


@pytest.fixture(scope="module")
def patterns():
    return load_patterns()


def matches(patterns, name: str, text: str) -> list[Reference]:
    pattern: CompiledPattern = patterns.by_name(name)
    return pattern.find(text, paragraph_idx=1)


def only(refs: list[Reference]) -> Reference:
    assert len(refs) == 1, f"expected exactly one reference, got {[r.raw_text for r in refs]}"
    return refs[0]


# --- loading ------------------------------------------------------------------------------


def test_pattern_set_is_a_singleton():
    assert pattern_set() is pattern_set()


def test_every_declared_pattern_is_compiled(patterns):
    names = {p.name for p in patterns.patterns}
    assert names == {"ref_no", "case_no_us", "case_no_gen", "ecli", "journal", "provision"}
    assert patterns.version == 1
    assert "tamtéž" in patterns.triggers
    assert patterns.markers["party_submission"]
    assert patterns.markers["departure"]
    assert patterns.markers["quashing"]


# --- ref_no -------------------------------------------------------------------------------


def test_ref_no_with_sheet_number(patterns):
    ref = only(matches(patterns, "ref_no", REF_NO_IN_SENTENCE))
    assert ref.kind is ReferenceKind.REF_NO
    assert ref.raw_text == "č. j. 6 Ads 45/2014-32"
    assert ref.groups == {"ref_no": "6 Ads 45/2014", "sheet_no": "32"}
    assert REF_NO_IN_SENTENCE[ref.start : ref.end] == ref.raw_text


def test_ref_no_without_sheet_number_omits_the_group(patterns):
    ref = only(matches(patterns, "ref_no", REF_NO_NO_SHEET))
    assert ref.groups == {"ref_no": "6 Ads 45/2014"}
    assert "sheet_no" not in ref.groups


def test_ref_no_needs_the_cj_marker(patterns):
    # A bare case number is a spisová značka, not a č. j.; ref_no must not claim it.
    assert matches(patterns, "ref_no", "6 Ads 45/2014") == []
    assert matches(patterns, "ref_no", CASE_NO_GEN) == []


# --- case_no ------------------------------------------------------------------------------


def test_case_no_gen(patterns):
    ref = only(matches(patterns, "case_no_gen", CASE_NO_GEN))
    assert ref.kind is ReferenceKind.CASE_NO
    assert ref.groups == {"case_no": "6 Ads 45/2014"}


def test_case_no_gen_does_not_match_a_constitutional_court_number(patterns):
    assert matches(patterns, "case_no_gen", CASE_NO_US) == []


def test_case_no_us(patterns):
    ref = only(matches(patterns, "case_no_us", CASE_NO_US))
    assert ref.kind is ReferenceKind.CASE_NO
    assert ref.groups == {"case_no": "II. ÚS 2379/08"}


def test_case_no_us_does_not_match_an_nss_number(patterns):
    assert matches(patterns, "case_no_us", CASE_NO_GEN) == []


def test_case_no_us_matches_the_plenary_form(patterns):
    """"Pl." replaces the roman numeral, it does not prefix one.

    PLAN.md section 7 stated this pattern as "(?:Pl\\.\\s*)?[IVX]+", which makes "Pl." an
    optional prefix to a *required* numeral, so no plenary number matched at all. Corrected
    to an alternation. This is not cosmetic: plénum decisions are what derogate a provision,
    and that derogation is the red light PLAN.md section 10 is built around.
    """
    ref = only(matches(patterns, "case_no_us", CASE_NO_US_PLENARY))
    assert ref.kind is ReferenceKind.CASE_NO
    assert ref.groups == {"case_no": "Pl. ÚS 29/98"}

    ref2 = only(matches(patterns, "case_no_us", CASE_NO_US_PLENARY_2))
    assert ref2.groups == {"case_no": "Pl. ÚS 36/93"}


def test_case_no_us_still_needs_a_panel_designator(patterns):
    """Widening the pattern for the plenum must not let a bare ÚS through."""
    assert matches(patterns, "case_no_us", CASE_NO_US_NO_DESIGNATOR) == []


# --- ecli ---------------------------------------------------------------------------------


def test_ecli(patterns):
    ref = only(matches(patterns, "ecli", ECLI))
    assert ref.kind is ReferenceKind.ECLI
    assert ref.raw_text == ECLI
    assert ref.groups == {"court": "NSS"}


def test_ecli_needs_a_four_digit_year(patterns):
    # Same identifier with the year truncated: a malformed ECLI must not match.
    assert matches(patterns, "ecli", "ECLI:CZ:NSS:15:6.Ads.45.2014.32") == []


# --- journal ------------------------------------------------------------------------------


def test_journal(patterns):
    ref = only(matches(patterns, "journal", JOURNAL))
    assert ref.kind is ReferenceKind.JOURNAL_NO
    assert ref.groups == {"journal_seq": "12", "journal_year": "2015"}


def test_journal_needs_a_space_and_a_four_digit_year(patterns):
    assert matches(patterns, "journal", "R12/2015") == []
    assert matches(patterns, "journal", "R 12/15") == []


# --- provision ----------------------------------------------------------------------------


def test_provision_with_act_number(patterns):
    ref = only(matches(patterns, "provision", PROVISION_WITH_ACT))
    assert ref.kind is ReferenceKind.PROVISION
    assert ref.groups == {"section": "2000", "subsec": "1", "act_no": "89/2012"}


def test_provision_without_act_number_leaves_it_unresolved(patterns):
    ref = only(matches(patterns, "provision", PROVISION_NO_ACT))
    assert ref.groups == {"section": "2000", "subsec": "1"}
    assert "act_no" not in ref.groups


def test_provision_without_subsection(patterns):
    ref = only(matches(patterns, "provision", PROVISION_BARE))
    assert ref.raw_text == "§17"
    assert ref.groups == {"section": "17"}


def test_provision_does_not_reach_across_a_full_stop_for_the_act(patterns):
    # The act tail is `[^.]{0,40}?`, so an abbreviation between section and act blocks it.
    # The section still matches; only the act number is (correctly) not claimed.
    ref = only(matches(patterns, "provision", "§ 2000 odst. 1 obč. zák. č. 89/2012 Sb."))
    assert ref.groups == {"section": "2000", "subsec": "1"}


def test_provision_needs_the_section_sign(patterns):
    assert matches(patterns, "provision", "2000 odst. 1") == []


# --- overlap policy -----------------------------------------------------------------------


def reference(start: int, end: int, kind: ReferenceKind = ReferenceKind.CASE_NO) -> Reference:
    return Reference(
        kind=kind, raw_text=CASE_NO_GEN[start:end], start=start, end=end, paragraph_idx=1
    )


def test_overlap_policy_drops_a_fully_contained_span():
    outer = reference(0, len(CASE_NO_GEN))  # "sp. zn. 6 Ads 45/2014"
    inner = reference(8, len(CASE_NO_GEN))  # "6 Ads 45/2014"
    assert drop_contained([outer, inner]) == [outer]
    assert drop_contained([inner, outer]) == [outer]


def test_overlap_policy_keeps_a_partial_overlap():
    left = reference(0, 12)
    right = reference(8, len(CASE_NO_GEN))
    assert drop_contained([left, right]) == [left, right]


def test_overlap_policy_breaks_an_exact_tie_by_toml_declaration_order():
    first = reference(0, 10, ReferenceKind.CASE_NO)
    second = reference(0, 10, ReferenceKind.REF_NO)
    assert drop_contained([first, second]) == [first]


def test_find_references_applies_the_overlap_policy(patterns):
    # The provision act tail swallows the journal citation sitting inside it; the longest
    # match wins, so only the provision survives.
    text = "§ 2000 odst. 1 R 12/2015 zákona č. 89/2012 Sb."
    refs = find_references(text, paragraph_idx=4, patterns=patterns)
    assert [r.kind for r in refs] == [ReferenceKind.PROVISION]
    assert refs[0].raw_text == text
    assert refs[0].paragraph_idx == 4


def test_find_references_returns_document_order(patterns):
    text = f"Viz {CASE_NO_GEN} a dále {PROVISION_WITH_ACT}"
    refs = find_references(text, paragraph_idx=1, patterns=patterns)
    assert [r.kind for r in refs] == [ReferenceKind.CASE_NO, ReferenceKind.PROVISION]
    assert refs[0].start < refs[1].start
    for ref in refs:
        assert text[ref.start : ref.end] == ref.raw_text

"""Unit tests for resolution: alias normalisation, provision anaphora, the model gate.

No database. Every function under test that needs one is split so that the decision-making
half is pure. Citation strings are quoted from PLAN.md / ``extract/patterns.toml``.
"""

from __future__ import annotations

import pytest

from jg.extract.anaphora import (
    build_prompt,
    candidate_sentences,
    filter_model_output,
    resolve_anaphora,
    split_sentences,
)
from jg.extract.patterns import find_references, load_patterns
from jg.extract.resolver import (
    BASIS_BACKWARD_SCAN,
    BASIS_DOCUMENT_MODE,
    BASIS_EXPLICIT,
    alias_candidates,
    assign_provision_acts,
    normalize_alias,
    provision_key,
    summarise,
)
from jg.models import Reference, ReferenceKind

ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32"
CASE_NO = "6 Ads 45/2014"
REF_NO = "6 Ads 45/2014"
SHEET_NO = "32"
JOURNAL = "R 12/2015"
ACT_NO = "89/2012"


@pytest.fixture(scope="module")
def patterns():
    return load_patterns()


def ref(kind: ReferenceKind, raw_text: str, paragraph_idx: int = 1, **groups: str) -> Reference:
    return Reference(
        kind=kind,
        raw_text=raw_text,
        start=0,
        end=len(raw_text),
        paragraph_idx=paragraph_idx,
        groups=groups,
    )


# --- normalisation ------------------------------------------------------------------------


def test_normalize_alias_collapses_whitespace_and_folds_case():
    assert normalize_alias("  6 Ads 45/2014-32 ") == "6 ads 45/2014-32"
    assert normalize_alias("6 Ads\n 45/2014") == "6 ads 45/2014"


def test_normalize_alias_is_idempotent():
    once = normalize_alias(f"  {ECLI}  ")
    assert normalize_alias(once) == once


def test_normalize_alias_keeps_the_sheet_number_distinct():
    # 6 Ads 45/2014 and 6 Ads 45/2014-32 are different aliases and must stay so.
    assert normalize_alias(CASE_NO) != normalize_alias(f"{REF_NO}-{SHEET_NO}")


# --- alias candidates ---------------------------------------------------------------------


def test_alias_candidates_for_ref_no_tries_the_sheet_form_first():
    reference = ref(
        ReferenceKind.REF_NO, f"č. j. {REF_NO}-{SHEET_NO}", ref_no=REF_NO, sheet_no=SHEET_NO
    )
    assert alias_candidates(reference) == ["6 ads 45/2014-32", "6 ads 45/2014"]


def test_alias_candidates_for_ref_no_without_sheet():
    reference = ref(ReferenceKind.REF_NO, f"č. j. {REF_NO}", ref_no=REF_NO)
    assert alias_candidates(reference) == ["6 ads 45/2014"]


def test_alias_candidates_for_case_no_and_ecli_and_journal():
    assert alias_candidates(ref(ReferenceKind.CASE_NO, f"sp. zn. {CASE_NO}", case_no=CASE_NO)) == [
        "6 ads 45/2014"
    ]
    assert alias_candidates(ref(ReferenceKind.ECLI, ECLI, court="NSS")) == [ECLI.casefold()]
    assert alias_candidates(
        ref(ReferenceKind.JOURNAL_NO, JOURNAL, journal_seq="12", journal_year="2015")
    ) == ["r 12/2015"]


def test_alias_candidates_for_a_provision_is_empty():
    assert alias_candidates(ref(ReferenceKind.PROVISION, "§ 2000 odst. 1", section="2000")) == []


# --- provision anaphora -------------------------------------------------------------------


def test_explicit_act_number_is_recorded_as_explicit(patterns):
    refs = find_references("§ 2000 odst. 1 zákona č. 89/2012 Sb.", 1, patterns)
    assign_provision_acts(refs)
    assert [r.resolution_basis for r in refs] == [BASIS_EXPLICIT]
    assert provision_key(refs[0]) == (ACT_NO, "2000", "1")


def test_missing_act_number_is_taken_from_the_most_recent_explicit_one(patterns):
    earlier = find_references("§ 2000 odst. 1 zákona č. 89/2012 Sb.", 1, patterns)
    later = find_references("Dále soud vyložil § 2000 odst. 1.", 7, patterns)
    refs = [*earlier, *later]
    assign_provision_acts(refs)
    assert later[0].resolution_basis == BASIS_BACKWARD_SCAN
    assert later[0].groups["act_no"] == ACT_NO
    assert provision_key(later[0]) == (ACT_NO, "2000", "1")


def test_an_act_number_appearing_only_later_falls_back_to_the_document_mode(patterns):
    first = find_references("Soud vyšel z § 2000 odst. 1.", 1, patterns)
    second = find_references("§ 2000 odst. 1 zákona č. 89/2012 Sb.", 5, patterns)
    refs = [*first, *second]
    assign_provision_acts(refs)
    assert first[0].resolution_basis == BASIS_DOCUMENT_MODE
    assert first[0].groups["act_no"] == ACT_NO
    assert second[0].resolution_basis == BASIS_EXPLICIT


def test_a_document_with_no_act_number_anywhere_stays_unresolved(patterns):
    refs = find_references("Podle §17 s.ř.s. platí, že §17 se použije obdobně.", 1, patterns)
    assign_provision_acts(refs)
    assert refs
    assert all(r.resolution_basis is None for r in refs)
    assert all("act_no" not in r.groups for r in refs)
    assert all(provision_key(r) is None for r in refs)


def test_anaphora_scan_is_ordered_by_paragraph_not_by_list_position(patterns):
    later = find_references("§ 2000 odst. 1", 9, patterns)
    earlier = find_references("§ 2000 odst. 1 zákona č. 89/2012 Sb.", 2, patterns)
    assign_provision_acts([*later, *earlier])  # deliberately out of document order
    assert later[0].resolution_basis == BASIS_BACKWARD_SCAN


# --- coverage stats -----------------------------------------------------------------------


def test_summarise_counts_resolved_and_unresolved_by_kind():
    resolved = ref(ReferenceKind.CASE_NO, f"sp. zn. {CASE_NO}", case_no=CASE_NO)
    resolved.resolved_ecli = ECLI
    unresolved = ref(ReferenceKind.CASE_NO, f"sp. zn. {CASE_NO}", 2, case_no=CASE_NO)
    provision = ref(ReferenceKind.PROVISION, "§ 2000 odst. 1", 3, section="2000")
    provision.resolution_basis = BASIS_DOCUMENT_MODE

    stats = summarise([resolved, unresolved, provision])
    assert stats.resolved["case_no"] == 1
    assert stats.unresolved["case_no"] == 1
    assert stats.unresolved["provision"] == 1
    assert stats.basis[BASIS_DOCUMENT_MODE] == 1
    assert stats.total == 3
    assert stats.coverage == pytest.approx(1 / 3)
    assert stats.coverage_for("case_no") == pytest.approx(0.5)


def test_empty_stats_report_full_coverage():
    stats = summarise([])
    assert stats.total == 0
    assert stats.coverage == 1.0


# --- anaphora selection and the model gate ------------------------------------------------


def test_split_sentences_does_not_break_on_legal_abbreviations():
    text = f"Soud odkázal na rozsudek ze dne 12. 3. 2015, č. j. {REF_NO}-{SHEET_NO}. Dále uvedl."
    sentences = [s for _, s in split_sentences(text)]
    assert len(sentences) == 2
    assert sentences[0].endswith("-32.")
    assert sentences[1].strip() == "Dále uvedl."


def test_split_sentence_offsets_index_back_into_the_paragraph():
    text = "První věta. Druhá věta."
    for start, sentence in split_sentences(text):
        assert text[start : start + len(sentence)] == sentence


def test_candidate_sentences_selects_triggers_without_a_rule_match(patterns):
    paragraphs = [
        f"Nejvyšší správní soud v rozsudku sp. zn. {CASE_NO} vyložil danou otázku.",
        "Druhý odstavec bez odkazu.",
        "Třetí odstavec bez odkazu.",
        "Soud setrvává na závěrech citovaného rozhodnutí.",
    ]
    refs = []
    for idx, body in enumerate(paragraphs, start=1):
        refs.extend(find_references(body, idx, patterns))

    candidates = candidate_sentences(paragraphs, refs, patterns)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.paragraph_idx == 4
    assert candidate.sentence == "Soud setrvává na závěrech citovaného rozhodnutí."
    # Context window: the sentence plus the preceding three paragraphs.
    assert candidate.context.startswith(paragraphs[0])
    assert candidate.context.endswith(candidate.sentence)
    assert paragraphs[1] in candidate.context and paragraphs[2] in candidate.context


def test_a_sentence_the_rules_pass_already_covered_is_not_a_candidate(patterns):
    paragraphs = [f"Ve smyslu citovaného rozhodnutí sp. zn. {CASE_NO} platí totéž."]
    refs = find_references(paragraphs[0], 1, patterns)
    assert refs
    assert candidate_sentences(paragraphs, refs, patterns) == []


def test_a_sentence_without_a_trigger_is_not_a_candidate(patterns):
    paragraphs = ["Soud se s tímto názorem neztotožnil."]
    assert candidate_sentences(paragraphs, [], patterns) == []


def resolved_reference(paragraph_idx: int = 1) -> Reference:
    reference = ref(ReferenceKind.CASE_NO, f"sp. zn. {CASE_NO}", paragraph_idx, case_no=CASE_NO)
    reference.resolved_ecli = ECLI
    return reference


#: Paragraph 1 names a decision; paragraph 2 refers back to it anaphorically.
ANAPHORIC_DOCUMENT = [
    f"Nejvyšší správní soud v rozsudku sp. zn. {CASE_NO} vyložil danou otázku.",
    "Soud setrvává na závěrech citovaného rozhodnutí.",
]


def test_model_output_naming_a_decision_from_this_document_is_kept():
    refs = [resolved_reference()]
    assert filter_model_output([ECLI], refs) == [ECLI]
    # An alias of the same reference is equally acceptable and normalises to the ECLI.
    assert filter_model_output([CASE_NO], refs) == [ECLI]
    assert filter_model_output([ECLI.casefold(), CASE_NO], refs) == [ECLI]


def test_model_output_naming_anything_else_is_discarded():
    refs = [resolved_reference()]
    assert filter_model_output(["TEST-NOT-IN-DOCUMENT"], refs) == []
    assert filter_model_output([""], refs) == []


def test_unresolved_references_are_not_offered_as_candidates():
    unresolved = ref(ReferenceKind.CASE_NO, f"sp. zn. {CASE_NO}", case_no=CASE_NO)
    assert filter_model_output([CASE_NO], [unresolved]) == []


def test_build_prompt_lists_only_candidates_from_this_document():
    candidate = candidate_sentences(
        ["Soud setrvává na závěrech citovaného rozhodnutí."], [], load_patterns()
    )[0]
    prompt = build_prompt(candidate, [resolved_reference()])
    assert ECLI in prompt
    assert candidate.sentence in prompt
    assert "TEST-NOT-IN-DOCUMENT" not in prompt


def test_resolve_anaphora_without_a_model_resolves_nothing():
    # The default must run with no API key: selection happens, resolution does not.
    assert candidate_sentences(ANAPHORIC_DOCUMENT, [resolved_reference()], load_patterns())
    assert resolve_anaphora(ANAPHORIC_DOCUMENT, [resolved_reference()]) == []


def test_resolve_anaphora_with_an_injected_model_filters_its_answers():
    refs = [resolved_reference()]
    seen: list[str] = []

    def model(prompt: str) -> list[str]:
        seen.append(prompt)
        return [ECLI, "TEST-HALLUCINATED"]

    resolutions = resolve_anaphora(ANAPHORIC_DOCUMENT, refs, model=model)
    assert len(seen) == 1
    assert ECLI in seen[0]
    assert [r.ecli for r in resolutions] == [ECLI]
    assert resolutions[0].paragraph_idx == 2

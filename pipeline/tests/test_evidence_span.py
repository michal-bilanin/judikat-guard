"""The evidence-span gate: CLAUDE.md rule 3 and PLAN.md D3, which are the same rule.

Every classification the model returns must quote the context it was given. The quote is
compared after whitespace normalisation and must be a literal substring; on failure the
call is retried **once** with the violation stated; on a second failure the row is written
``UNCLASSIFIED``. An unvalidated label is never stored, and no test here is allowed to
relax that in order to pass.

The model is an injectable callable, so all of this runs with no API key and no network.
The counting matters as much as the outcome: exactly one retry, exactly two calls.

Identifiers: the cited ECLI is the one in PLAN.md section 11; the citing decision carries a
``TEST-`` prefix that cannot resolve (CLAUDE.md rule 1).
"""

from __future__ import annotations

import datetime as dt

import pytest

from jg.classify.prompts import (
    PromptError,
    UnsubstitutedPlaceholder,
    load_prompt,
    parse_prompt,
    prompt_template,
    split_frontmatter,
)
from jg.classify.reasoning import (
    NO_RATIO,
    PROMPT_NAME,
    MemoryCache,
    ResponseRejected,
    build_prompt,
    cache_key,
    classify_edge,
    is_verbatim,
    parse_response,
)
from jg.classify.structural import CitationEdge
from jg.models import PanelType, Route, TreatmentLabel

REF_NO = "č. j. 6 Ads 45/2014-32"
CITED_ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32"
CITING_ECLI = "TEST-citing-decision"

BEFORE = "K namítanému nesprávnému výkladu se soud vyjadřuje níže."
#: The focus paragraph. Contains a non-breaking space between "12." and "3.", exactly as
#: court publication systems emit dates, so the gate is exercised on a real difference.
FOCUS = (
    "Nejvyšší správní soud vychází z rozsudku ze dne 12.\xa03.\xa02015, "
    f"{REF_NO}, jehož závěry však platí pouze pro věci nemovité."
)
AFTER = "Z těchto důvodů soud kasační stížnost zamítl."

RATIO = "Závěr o povinnosti platí i pro movité věci."


def edge(**overrides: object) -> CitationEdge:
    fields: dict[str, object] = {
        "citation_id": 7,
        "citing_ecli": CITING_ECLI,
        "citing_court": "NSS",
        "citing_panel": PanelType.EXTENDED,
        "citing_date": dt.date(2026, 9, 1),
        "cited_ecli": CITED_ECLI,
        "cited_court": "NSS",
        "cited_date": dt.date(2015, 3, 12),
        "paragraph_idx": 34,
        "raw_text": REF_NO,
        "context": (BEFORE, FOCUS, AFTER),
        "context_focus": 1,
        "cited_ratio": RATIO,
    }
    fields.update(overrides)
    return CitationEdge(**fields)  # type: ignore[arg-type]


NARROWING_SPAN = "jehož závěry však platí pouze pro věci nemovité"


def answer(span: str, label: str = "NARROWED", confidence: float = 0.82) -> dict[str, object]:
    return {
        "label": label,
        "confidence": confidence,
        "evidence_span": span,
        "reasoning": "Soud omezil rozsah dosavadního závěru.",
    }


class ScriptedModel:
    """Returns the queued responses in order and records every prompt it was sent."""

    def __init__(self, *responses: dict[str, object]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> dict[str, object]:
        self.prompts.append(prompt)
        if not self._responses:
            raise AssertionError("the model was called more times than the test allows")
        return self._responses.pop(0)

    @property
    def calls(self) -> int:
        return len(self.prompts)


# --- the substring gate ------------------------------------------------------------------


def test_is_verbatim_accepts_an_exact_quote():
    assert is_verbatim(NARROWING_SPAN, FOCUS)


def test_is_verbatim_accepts_a_quote_retyped_with_plain_spaces():
    """A model that copies the passage correctly still retypes a non-breaking space."""
    quoted = "rozsudku ze dne 12. 3. 2015"
    assert "12.\xa03.\xa02015" in FOCUS
    assert "\xa0" not in quoted
    assert is_verbatim(quoted, FOCUS)


def test_is_verbatim_accepts_a_quote_broken_across_lines():
    assert is_verbatim("jehož závěry\n   však platí", FOCUS)


def test_is_verbatim_rejects_a_paraphrase():
    assert not is_verbatim("závěry platí jen pro nemovitosti", FOCUS)


def test_is_verbatim_rejects_an_empty_span():
    assert not is_verbatim("   ", FOCUS)


def test_is_verbatim_is_case_sensitive():
    """Diacritics and case carry the meaning the label rests on; only whitespace is folded."""
    assert not is_verbatim(NARROWING_SPAN.upper(), FOCUS)


# --- response validation -----------------------------------------------------------------


def test_parse_response_accepts_a_well_formed_answer():
    label, confidence, span = parse_response(answer(NARROWING_SPAN), FOCUS)
    assert label is TreatmentLabel.NARROWED
    assert confidence == 0.82
    assert span == NARROWING_SPAN


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (answer(NARROWING_SPAN, label="OVERRULED"), "label"),
        (answer(NARROWING_SPAN, label="UNCLASSIFIED"), "label"),
        (answer(NARROWING_SPAN, confidence=1.4), "confidence"),
        ({"label": "NARROWED", "evidence_span": NARROWING_SPAN}, "confidence"),
        (answer(""), "evidence_span"),
        (answer("soud tento závěr výslovně překonal"), "evidence_span"),
    ],
)
def test_parse_response_rejects_a_malformed_answer(payload, expected):
    with pytest.raises(ResponseRejected) as raised:
        parse_response(payload, FOCUS)
    assert expected in raised.value.violation


def test_the_model_may_not_claim_unclassified_itself():
    """``UNCLASSIFIED`` is this stage's verdict on the model, not a label it may return."""
    with pytest.raises(ResponseRejected):
        parse_response(answer(NARROWING_SPAN, label="UNCLASSIFIED"), FOCUS)


# --- one call, one retry, then UNCLASSIFIED ----------------------------------------------


def test_a_valid_first_answer_is_stored_as_is():
    model = ScriptedModel(answer(NARROWING_SPAN))
    result = classify_edge(edge(), model=model, model_name="TEST-model")

    assert model.calls == 1
    assert result.label is TreatmentLabel.NARROWED
    assert result.confidence == 0.82
    assert result.evidence_span == NARROWING_SPAN
    assert result.route is Route.REASONING
    assert result.model == "TEST-model"
    assert result.prompt_version == prompt_template(PROMPT_NAME).version


def test_a_bad_span_is_retried_exactly_once_with_the_violation_stated():
    hallucinated = "soud se od tohoto závěru odchýlil"
    model = ScriptedModel(answer(hallucinated), answer(NARROWING_SPAN))
    result = classify_edge(edge(), model=model, model_name="TEST-model")

    assert model.calls == 2
    retry = model.prompts[1]
    assert "rejected" in retry
    assert hallucinated in retry
    assert "character for character" in retry
    #: The retry is the same prompt plus the violation; the frozen template is untouched.
    assert retry.startswith(model.prompts[0])
    assert result.label is TreatmentLabel.NARROWED
    assert result.evidence_span == NARROWING_SPAN


def test_two_bad_spans_fall_through_to_unclassified():
    model = ScriptedModel(
        answer("soud se od tohoto závěru odchýlil"),
        answer("tento výklad byl překonán"),
    )
    result = classify_edge(edge(), model=model, model_name="TEST-model")

    assert model.calls == 2
    assert result.label is TreatmentLabel.UNCLASSIFIED
    assert result.confidence == 0.0
    assert result.route is Route.REASONING
    assert result.model == "TEST-model"
    #: The stored span is the sentence we asked about, quoted from the paragraph, so the
    #: amber "needs review" can still point a human at the place. Not a model claim.
    assert result.evidence_span in " ".join(FOCUS.split())
    assert REF_NO.split()[-1] in result.evidence_span


def test_an_unclassified_row_still_carries_a_span_when_the_paragraph_is_missing():
    model = ScriptedModel(answer("nesmysl"), answer("stále nesmysl"))
    result = classify_edge(
        edge(context=(), context_focus=0), model=model, model_name="TEST-model"
    )
    assert result.label is TreatmentLabel.UNCLASSIFIED
    assert result.evidence_span == REF_NO


# --- caching -----------------------------------------------------------------------------


def test_cache_key_is_the_documented_tuple():
    template = prompt_template(PROMPT_NAME)
    key = cache_key(CITING_ECLI, CITED_ECLI, 34, template.version)
    assert key.split("|") == ["treatment", CITING_ECLI, CITED_ECLI, "34", template.version]
    assert cache_key(CITING_ECLI, CITED_ECLI, None, template.version).endswith(
        f"|-|{template.version}"
    )


def test_a_cache_hit_makes_no_model_call():
    cache = MemoryCache()
    first = ScriptedModel(answer(NARROWING_SPAN))
    classify_edge(edge(), model=first, model_name="TEST-model", cache=cache)
    assert first.calls == 1

    second = ScriptedModel()  # any call at all raises
    result = classify_edge(edge(), model=second, model_name="TEST-model", cache=cache)
    assert second.calls == 0
    assert result.label is TreatmentLabel.NARROWED


def test_a_rejected_answer_is_not_cached():
    """Otherwise a fixed prompt could never re-ask an edge the model once got wrong."""
    cache = MemoryCache()
    model = ScriptedModel(answer("nesmysl"), answer("stále nesmysl"))
    classify_edge(edge(), model=model, model_name="TEST-model", cache=cache)
    template = prompt_template(PROMPT_NAME)
    assert cache.get(cache_key(CITING_ECLI, CITED_ECLI, 34, template.version)) is None


def test_a_poisoned_cache_entry_is_revalidated_and_re_asked():
    cache = MemoryCache()
    template = prompt_template(PROMPT_NAME)
    key = cache_key(CITING_ECLI, CITED_ECLI, 34, template.version)
    cache.put(
        key,
        request={},
        response=answer("závěr, který v kontextu vůbec není"),
        model="TEST-model",
        prompt_version=template.version,
    )
    model = ScriptedModel(answer(NARROWING_SPAN))
    result = classify_edge(edge(), model=model, model_name="TEST-model", cache=cache)
    assert model.calls == 1
    assert result.label is TreatmentLabel.NARROWED


# --- the prompt itself -------------------------------------------------------------------


def test_the_frozen_template_declares_its_version_and_placeholders():
    template = load_prompt(PROMPT_NAME)
    assert template.version == "treatment-classify.v1"
    assert set(template.placeholders()) == set(template.declared_placeholders)
    assert template.metadata["cache_key"] == (
        "(citing_ecli, cited_ecli, paragraph_idx, prompt_version)"
    )


def test_build_prompt_supplies_the_context_the_gate_will_check_against():
    prompt = build_prompt(edge())
    assert edge().context_text in prompt
    assert RATIO in prompt
    assert "NSS" in prompt
    assert "2015-03-12" in prompt
    assert "{{" not in prompt


def test_build_prompt_states_a_missing_ratio_rather_than_leaving_it_blank():
    assert NO_RATIO in build_prompt(edge(cited_ratio=None))


def test_render_refuses_a_prompt_with_an_unsubstituted_placeholder():
    template = parse_prompt(
        "---\nversion: TEST-prompt.v1\n---\nA {{one}} and a {{two}}.",
        name="TEST-prompt.v1",
        path=load_prompt(PROMPT_NAME).path,
    )
    with pytest.raises(UnsubstitutedPlaceholder):
        template.render(one="x")
    assert template.render(one="x", two="y") == "A x and a y."


def test_render_refuses_an_unknown_placeholder_and_a_none_value():
    template = parse_prompt(
        "---\nversion: TEST-prompt.v1\n---\nA {{one}}.",
        name="TEST-prompt.v1",
        path=load_prompt(PROMPT_NAME).path,
    )
    with pytest.raises(PromptError):
        template.render(one="x", three="y")
    with pytest.raises(PromptError):
        template.render(one=None)


def test_a_template_without_a_version_is_unusable():
    with pytest.raises(PromptError):
        parse_prompt(
            "---\ncache_key: none\n---\nbody",
            name="TEST-prompt.v1",
            path=load_prompt(PROMPT_NAME).path,
        )


def test_split_frontmatter_reads_scalars_and_lists():
    front, body = split_frontmatter(
        "---\nversion: v1\nplaceholders:\n  - a\n  - b\n---\nbody text\n"
    )
    assert front == {"version": "v1", "placeholders": ["a", "b"]}
    assert body.strip() == "body text"


def test_html_comments_are_stripped_from_the_body():
    """The frozen template documents its own ``{{name}}`` syntax inside a comment."""
    assert "FROZEN" not in load_prompt(PROMPT_NAME).body

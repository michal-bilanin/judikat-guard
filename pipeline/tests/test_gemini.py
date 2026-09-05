"""The Gemini provider: wire shape, 429 handling, and both seams end to end.

Nothing here touches the network and nothing here needs an API key: every request goes
through ``httpx.MockTransport`` and every sleep goes through an injected recorder, so the
retry tests are instant. The transport also asserts the request, which is the point — the
Interactions API replaced the older ``generateContent`` + ``contents``/``parts`` shape, and
a provider that silently reverts to a remembered shape would fail only against the live
API, with a key, at cost.

The last third of the file is the part that matters most: the evidence-span gate
(CLAUDE.md rule 3) lives *above* this seam, so it must behave identically whichever
provider answered. It is exercised here through the Gemini path — a bad span is retried
once and a second bad span writes ``UNCLASSIFIED`` — and the model ID recorded on the row
is the Gemini one, so a corpus holding rows from both providers stays unambiguous.

Identifiers: the cited ECLI is the one PLAN.md section 11 uses; the citing decision carries
a ``TEST-`` prefix that cannot resolve (CLAUDE.md rule 1).
"""

from __future__ import annotations

import datetime as dt
import json
import random
from typing import Any

import httpx
import pytest

from jg.classify.reasoning import (
    RESPONSE_SCHEMA,
    MemoryCache,
    ModelUnavailable,
    cache_key,
    classify_edge,
    prompt_template,
)
from jg.classify.structural import CitationEdge
from jg.config import (
    ANTHROPIC,
    GEMINI,
    GEMINI_MIN_INTERVAL_DEFAULT,
    GEMINI_MODEL_DEFAULT,
    gemini_api_key,
    gemini_min_interval,
    gemini_model_name,
    llm_model_name,
    llm_provider,
    provider_api_key,
)
from jg.gemini import (
    API_KEY_HEADER,
    API_URL,
    BACKOFF_CAP_SECONDS,
    backoff_seconds,
    extract_text,
    gemini_model,
    provider_model,
    retry_after_seconds,
)
from jg.models import PanelType, Route, TreatmentLabel
from jg.provisions import RESPONSE_SCHEMA as MATERIALITY_SCHEMA

KEY = "TEST-not-a-real-key"
ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "JG_LLM_PROVIDER",
            "JG_GEMINI_MODEL", "JG_LLM_MODEL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No provider environment leaks into a test from the shell that ran pytest."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------- the transport


class Recorder:
    """A ``MockTransport`` handler: replays queued responses, records every request."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("the transport was called more times than the test allows")
        return self._responses.pop(0)

    @property
    def calls(self) -> int:
        return len(self.requests)

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


class Clock:
    """An injected sleep. Records what would have been waited; waits nothing."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def interaction(payload: dict[str, Any] | str, *, status: str = "completed") -> httpx.Response:
    """A 200 in the documented ``steps`` shape, carrying ``payload`` as the model output."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return httpx.Response(
        200,
        json={
            "id": "int_TEST",
            "status": status,
            "steps": [
                {
                    "type": "user_input",
                    "status": "done",
                    "content": [{"type": "text", "text": "..."}],
                },
                {
                    "type": "model_output",
                    "status": "done",
                    "content": [{"type": "text", "text": text}],
                },
            ],
        },
    )


def build(recorder: Recorder, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "schema": RESPONSE_SCHEMA,
        "api_key": KEY,
        "client": recorder.client(),
        "sleep": Clock(),
        # Pacing off by default here so that a test asserting on the recorded sleeps sees
        # only the retry backoff it is about. The pacing schedule has its own tests below.
        "min_interval": 0.0,
    }
    kwargs.update(overrides)
    return gemini_model(**kwargs)


# ------------------------------------------------------------------- extract_text


def test_extract_text_reads_the_model_output_step():
    body = interaction({"label": "FOLLOWED"}).json()
    assert extract_text(body) == '{"label": "FOLLOWED"}'


def test_extract_text_concatenates_text_blocks_and_ignores_the_rest():
    body = {
        "id": "int_TEST",
        "status": "completed",
        "steps": [
            {"type": "user_input", "content": [{"type": "text", "text": "IGNORED"}]},
            {
                "type": "model_output",
                "content": [
                    {"type": "thought", "text": "IGNORED"},
                    {"type": "text", "text": '{"label":'},
                    {"type": "text", "text": ' "FOLLOWED"}'},
                ],
            },
        ],
    }
    assert extract_text(body) == '{"label": "FOLLOWED"}'


def test_extract_text_falls_back_to_output_text():
    """The convenience property Google's SDKs expose, in case it appears on the wire."""
    assert extract_text({"id": "int_TEST", "output_text": '{"label": "FOLLOWED"}'}) == (
        '{"label": "FOLLOWED"}'
    )


def test_extract_text_ignores_an_empty_model_output_and_still_finds_output_text():
    body = {
        "status": "completed",
        "steps": [{"type": "model_output", "content": []}],
        "output_text": "{}",
    }
    assert extract_text(body) == "{}"


def test_extract_text_raises_naming_what_it_saw():
    """A body in no recognised shape must never come back as an empty string."""
    legacy = {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
    with pytest.raises(ModelUnavailable) as caught:
        extract_text(legacy)
    message = str(caught.value)
    assert "candidates" in message
    assert "no model output text" in message


def test_extract_text_quotes_the_status_of_a_failed_interaction():
    with pytest.raises(ModelUnavailable) as caught:
        extract_text({"id": "int_TEST", "status": "failed", "steps": []})
    assert "'failed'" in str(caught.value)


# ------------------------------------------------------------------ the request body


def test_the_request_is_the_documented_interactions_shape():
    recorder = Recorder(interaction({"ok": True}))
    build(recorder, model="gemini-3.5-flash-lite")("PROMPT")

    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == API_URL
    assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/interactions"
    # The key is a header, and it is nowhere near the URL.
    assert request.headers[API_KEY_HEADER] == KEY
    assert "key" not in request.url.params
    assert KEY not in str(request.url)
    assert request.headers["content-type"] == "application/json"

    assert recorder.body() == {
        "model": "gemini-3.5-flash-lite",
        "input": "PROMPT",
        "generation_config": {"temperature": 0, "thinking_level": "minimal"},
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": RESPONSE_SCHEMA,
        },
    }


def test_temperature_zero_is_sent_explicitly():
    """CLAUDE.md rule 7 to the letter: Gemini accepts the parameter, so it is sent."""
    recorder = Recorder(interaction({"ok": True}))
    build(recorder)("PROMPT")
    config = recorder.body()["generation_config"]
    assert config["temperature"] == 0
    assert "temperature" not in recorder.body()  # nests in generation_config, not top level


def test_the_model_defaults_to_flash_lite_and_is_overridable(monkeypatch: pytest.MonkeyPatch):
    recorder = Recorder(interaction({"ok": True}))
    build(recorder)("PROMPT")
    assert recorder.body()["model"] == GEMINI_MODEL_DEFAULT == "gemini-3.5-flash-lite"

    monkeypatch.setenv("JG_GEMINI_MODEL", "gemini-3.5-flash")
    other = Recorder(interaction({"ok": True}))
    build(other)("PROMPT")
    assert other.body()["model"] == "gemini-3.5-flash"


def test_one_factory_serves_the_materiality_schema_too():
    """M5 and M6 differ only by schema, so there is one client, not two."""
    recorder = Recorder(interaction({"material": True}))
    build(recorder, schema=MATERIALITY_SCHEMA)("PROMPT")
    assert recorder.body()["response_format"]["schema"] == MATERIALITY_SCHEMA
    assert recorder.body()["response_format"]["schema"] != RESPONSE_SCHEMA


def test_no_key_is_a_named_error_not_a_traceback():
    with pytest.raises(ModelUnavailable) as caught:
        gemini_model(schema=RESPONSE_SCHEMA, api_key=None)
    assert "GEMINI_API_KEY" in str(caught.value)
    assert "GOOGLE_API_KEY" in str(caught.value)


def test_an_unknown_thinking_level_is_refused_before_any_request():
    recorder = Recorder()
    with pytest.raises(ModelUnavailable) as caught:
        build(recorder, thinking_level="off")
    assert "minimal" in str(caught.value)
    assert recorder.calls == 0


# ------------------------------------------------------------------------ retrying


def test_a_429_with_retry_after_is_retried_and_then_succeeds():
    recorder = Recorder(
        httpx.Response(429, headers={"retry-after": "2"}, text="rate limit"),
        interaction({"label": "FOLLOWED"}),
    )
    clock = Clock()
    result = build(recorder, sleep=clock)("PROMPT")

    assert result == {"label": "FOLLOWED"}
    assert recorder.calls == 2
    assert clock.slept == [2.0]  # honoured exactly, and not actually waited


def test_the_retried_request_is_identical():
    recorder = Recorder(httpx.Response(429), interaction({"label": "FOLLOWED"}))
    build(recorder, sleep=Clock())("PROMPT")
    assert recorder.body(0) == recorder.body(1)


def test_a_429_without_retry_after_backs_off_exponentially():
    recorder = Recorder(
        httpx.Response(429), httpx.Response(429), interaction({"label": "FOLLOWED"})
    )
    clock = Clock()
    # A seeded Random keeps the jitter reproducible; the schedule, not the draw, is asserted.
    build(recorder, sleep=clock, rng=random.Random(1))("PROMPT")
    assert len(clock.slept) == 2
    assert 0.0 <= clock.slept[0] <= 1.0
    assert 0.0 <= clock.slept[1] <= 2.0


def test_a_retry_delay_stated_in_the_error_body_is_honoured():
    """Google's REST errors carry the wait as a RetryInfo detail as well as a header."""
    recorder = Recorder(
        httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                         "retryDelay": "27s"}
                    ],
                }
            },
        ),
        interaction({"label": "FOLLOWED"}),
    )
    clock = Clock()
    build(recorder, sleep=clock)("PROMPT")
    assert clock.slept == [27.0]


def test_a_daily_cap_retry_after_is_capped_rather_than_slept_out():
    recorder = Recorder(httpx.Response(429, headers={"retry-after": "86400"}),
                        interaction({"label": "FOLLOWED"}))
    clock = Clock()
    build(recorder, sleep=clock)("PROMPT")
    assert clock.slept == [300.0]


def test_a_5xx_is_retried():
    recorder = Recorder(httpx.Response(503, text="unavailable"), interaction({"ok": True}))
    assert build(recorder, sleep=Clock())("PROMPT") == {"ok": True}


def test_exhausted_retries_fail_with_a_clear_error_naming_the_status():
    recorder = Recorder(*[httpx.Response(429, text="quota") for _ in range(3)])
    with pytest.raises(ModelUnavailable) as caught:
        build(recorder, sleep=Clock(), max_retries=2)("PROMPT")
    assert "429" in str(caught.value)
    assert "quota" in str(caught.value)
    assert recorder.calls == 3


def test_a_400_is_not_retried_and_reports_the_api_error_verbatim():
    """A schema Gemini rejects has to be readable in the message, not swallowed."""
    recorder = Recorder(
        httpx.Response(400, json={"error": {"message": "Invalid JSON schema: too deep"}})
    )
    with pytest.raises(ModelUnavailable) as caught:
        build(recorder, sleep=Clock())("PROMPT")
    assert "400" in str(caught.value)
    assert "Invalid JSON schema: too deep" in str(caught.value)
    assert recorder.calls == 1


def test_a_transport_error_is_retried_then_named():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    clock = Clock()
    call = gemini_model(
        schema=RESPONSE_SCHEMA,
        api_key=KEY,
        client=client,
        sleep=clock,
        max_retries=2,
        min_interval=0.0,  # backoff only, so the recorded sleeps are the retries
    )
    with pytest.raises(ModelUnavailable) as caught:
        call("PROMPT")
    assert "no route to host" in str(caught.value)
    assert len(clock.slept) == 2


def test_output_that_is_not_json_is_a_named_error_not_an_empty_answer():
    recorder = Recorder(interaction("Omlouvám se, ale nemohu odpovědět."))
    with pytest.raises(ModelUnavailable) as caught:
        build(recorder, sleep=Clock())("PROMPT")
    assert "Omlouvám se" in str(caught.value)


def test_backoff_is_bounded_and_retry_after_parsing_is_tolerant():
    assert 0.0 <= backoff_seconds(20) <= BACKOFF_CAP_SECONDS
    assert retry_after_seconds(httpx.Response(429)) is None
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": "nonsense"})) is None
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": "5"})) == 5.0


# --------------------------------------------------------------- provider selection


def test_the_provider_is_auto_detected_from_whichever_key_is_present(
    monkeypatch: pytest.MonkeyPatch,
):
    assert llm_provider() == ANTHROPIC  # neither key: unchanged behaviour
    assert provider_api_key() is None

    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert llm_provider() == ANTHROPIC

    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    assert llm_provider() == GEMINI
    assert provider_api_key() == KEY


def test_an_explicit_provider_always_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("JG_LLM_PROVIDER", "anthropic")
    assert llm_provider() == ANTHROPIC
    assert provider_api_key() is None  # ANTHROPIC_API_KEY is what would be needed


def test_google_api_key_is_read_as_a_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_API_KEY", KEY)
    assert gemini_api_key() == KEY
    assert llm_provider() == GEMINI


def test_the_recorded_model_name_follows_the_provider(monkeypatch: pytest.MonkeyPatch):
    assert llm_model_name(ANTHROPIC) == "claude-sonnet-5"
    assert llm_model_name(GEMINI) == GEMINI_MODEL_DEFAULT
    monkeypatch.setenv("JG_GEMINI_MODEL", "gemini-3.8-flash")
    assert gemini_model_name() == "gemini-3.8-flash"
    assert llm_model_name(GEMINI) == "gemini-3.8-flash"


def test_provider_model_dispatches_to_each_provider(monkeypatch: pytest.MonkeyPatch):
    built: list[str] = []

    def fake_anthropic(**kwargs: Any):
        built.append("anthropic")
        return lambda prompt: {}

    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    recorder = Recorder(interaction({"ok": True}))
    call = provider_model(
        RESPONSE_SCHEMA, anthropic=fake_anthropic, provider=GEMINI, client=recorder.client()
    )
    assert call("PROMPT") == {"ok": True}
    assert built == []

    provider_model(RESPONSE_SCHEMA, anthropic=fake_anthropic, provider=ANTHROPIC)
    assert built == ["anthropic"]


def test_a_misspelled_provider_is_refused_rather_than_billed_elsewhere():
    with pytest.raises(ModelUnavailable) as caught:
        provider_model(RESPONSE_SCHEMA, anthropic=lambda **_: None, provider="gemeni")
    assert "gemeni" in str(caught.value)


# ------------------------------------------------- M5 end to end, through the gate

REF_NO = "č. j. 6 Ads 45/2014-32"
CITED_ECLI = "ECLI:CZ:NSS:2015:6.Ads.45.2014.32"
CITING_ECLI = "TEST-citing-decision"

FOCUS = (
    "Nejvyšší správní soud vychází z rozsudku ze dne 12.\xa03.\xa02015, "
    f"{REF_NO}, jehož závěry však platí pouze pro věci nemovité."
)
NARROWING_SPAN = "jehož závěry však platí pouze pro věci nemovité"

EDGE = CitationEdge(
    citation_id=7,
    citing_ecli=CITING_ECLI,
    citing_court="NSS",
    citing_panel=PanelType.EXTENDED,
    citing_date=dt.date(2026, 9, 1),
    cited_ecli=CITED_ECLI,
    cited_court="NSS",
    cited_date=dt.date(2015, 3, 12),
    paragraph_idx=34,
    raw_text=REF_NO,
    context=("K namítanému výkladu se soud vyjadřuje níže.", FOCUS, "Soud stížnost zamítl."),
    context_focus=1,
    cited_ratio="Závěr o povinnosti platí i pro movité věci.",
)


def answer(span: str) -> dict[str, Any]:
    return {
        "label": "NARROWED",
        "confidence": 0.82,
        "evidence_span": span,
        "reasoning": "Soud omezil rozsah dosavadního závěru.",
    }


def test_a_gemini_answer_flows_through_classify_edge_to_a_storable_row():
    recorder = Recorder(interaction(answer(NARROWING_SPAN)))
    result = classify_edge(
        EDGE,
        model=build(recorder, model="gemini-3.5-flash-lite"),
        model_name="gemini-3.5-flash-lite",
    )

    assert result.label is TreatmentLabel.NARROWED
    assert result.confidence == 0.82
    assert result.evidence_span == NARROWING_SPAN
    assert result.route is Route.REASONING
    # Rule 7: the row names the model that actually answered, so a corpus holding rows from
    # both providers stays unambiguous.
    assert result.model == "gemini-3.5-flash-lite"
    assert result.prompt_version == prompt_template("treatment-classify.v1").version
    # The prompt really was the rendered template, not something this test made up.
    assert NARROWING_SPAN in recorder.body()["input"]


def test_the_span_gate_still_retries_once_over_the_gemini_path():
    recorder = Recorder(
        interaction(answer("soud rozhodl zcela jinak")),
        interaction(answer(NARROWING_SPAN)),
    )
    result = classify_edge(EDGE, model=build(recorder), model_name=GEMINI_MODEL_DEFAULT)

    assert result.label is TreatmentLabel.NARROWED
    assert recorder.calls == 2
    assert "Your previous answer was rejected" in recorder.body(1)["input"]


def test_two_bad_spans_over_the_gemini_path_are_unclassified():
    """CLAUDE.md rule 3, unchanged by the provider: the gate is above this seam."""
    recorder = Recorder(
        interaction(answer("soud rozhodl zcela jinak")),
        interaction(answer("a znovu něco jiného")),
    )
    result = classify_edge(EDGE, model=build(recorder), model_name=GEMINI_MODEL_DEFAULT)

    assert result.label is TreatmentLabel.UNCLASSIFIED
    assert result.confidence == 0.0
    assert result.evidence_span in " ".join(FOCUS.split())
    assert result.model == GEMINI_MODEL_DEFAULT
    assert recorder.calls == 2


def test_a_cache_hit_costs_no_gemini_request():
    """The resumed-run guarantee: an already-classified edge is not re-paid for."""
    cache = MemoryCache()
    version = prompt_template("treatment-classify.v1").version
    key = cache_key(CITING_ECLI, CITED_ECLI, 34, version)
    cache.put(
        key,
        request={},
        response=answer(NARROWING_SPAN),
        model=GEMINI_MODEL_DEFAULT,
        prompt_version=version,
    )

    recorder = Recorder()  # any request at all raises
    result = classify_edge(
        EDGE, model=build(recorder), model_name=GEMINI_MODEL_DEFAULT, cache=cache
    )
    assert result.label is TreatmentLabel.NARROWED
    assert recorder.calls == 0


def test_a_validated_answer_is_cached_for_the_next_run():
    cache = MemoryCache()
    recorder = Recorder(interaction(answer(NARROWING_SPAN)))
    classify_edge(EDGE, model=build(recorder), model_name=GEMINI_MODEL_DEFAULT, cache=cache)

    version = prompt_template("treatment-classify.v1").version
    assert cache.get(cache_key(CITING_ECLI, CITED_ECLI, 34, version)) is not None


# ------------------------------------------------------------------- proactive pacing


ANSWER = {"label": "FOLLOWED", "confidence": 0.9, "evidence_span": "x", "reasoning": "y"}


def test_requests_are_paced_to_stay_inside_the_free_tier_rate_limit():
    """The client waits between requests rather than discovering the limit with a 429.

    The free tier allows roughly ten requests a minute and the queued M5 workload is over a
    thousand calls, so reacting to 429s alone would mean a rejected round trip plus a backoff
    for a large share of them. Both the clock and the sleep are injected, so this pins the
    schedule without spending a real second.
    """
    now = [1000.0]
    slept: list[float] = []

    def wait(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    call = gemini_model(
        schema=RESPONSE_SCHEMA,
        api_key=KEY,
        client=httpx.Client(transport=httpx.MockTransport(lambda _r: interaction(ANSWER))),
        min_interval=6.0,
        sleep=wait,
        clock=lambda: now[0],
    )

    call("first")
    assert slept == [], "the first request has nothing to wait for"

    call("second")
    assert slept == [6.0], "the second waits out the whole interval"

    now[0] += 4.0  # four seconds of real work happened in between
    call("third")
    assert slept == [6.0, 2.0], "only the remainder of the interval is waited out"


def test_pacing_can_be_switched_off_for_a_paid_tier():
    """``JG_GEMINI_MIN_INTERVAL=0``: off the free tier the gap is pure lost throughput."""
    slept: list[float] = []
    call = gemini_model(
        schema=RESPONSE_SCHEMA,
        api_key=KEY,
        client=httpx.Client(transport=httpx.MockTransport(lambda _r: interaction(ANSWER))),
        min_interval=0.0,
        sleep=slept.append,
        clock=lambda: 0.0,
    )
    call("a")
    call("b")
    assert slept == []


def test_the_pacing_interval_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JG_GEMINI_MIN_INTERVAL", "2.5")
    assert gemini_min_interval() == 2.5
    monkeypatch.setenv("JG_GEMINI_MIN_INTERVAL", "not-a-number")
    assert gemini_min_interval() == GEMINI_MIN_INTERVAL_DEFAULT, "a typo must not fail a batch"
    monkeypatch.delenv("JG_GEMINI_MIN_INTERVAL")
    assert gemini_min_interval() == GEMINI_MIN_INTERVAL_DEFAULT

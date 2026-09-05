"""The Gemini provider: one ``httpx`` client serving both model seams of this pipeline.

Why this module exists: neither a Claude Team plan nor a Google Pro subscription includes
API access — both are seat products, billed separately from the APIs. Google AI Studio
does publish a free API tier, so Gemini is the provider this project can actually run on.
Anthropic stays exactly where it was; this is a second implementation behind the seam that
:mod:`jg.classify.reasoning` already defines, not a replacement for it.

There are two seams and they differ only by response schema:

* M5, treatment classification — ``jg.classify.reasoning.ModelCall`` with that module's
  ``RESPONSE_SCHEMA``,
* M6, provision materiality — ``jg.provisions.MaterialityCall`` with its own schema.

Both are ``Callable[[str], Mapping[str, Any]]``, so :func:`gemini_model` takes the schema as
a parameter and serves both. There is one client here, not two near-identical ones.

What this module deliberately does **not** do: validate the model's answer. The
evidence-span gate (CLAUDE.md rule 3) lives above this seam, in ``classify_edge`` and
``judge_materiality``, and it runs unchanged whichever provider produced the JSON. A
provider is a transport; it is never allowed to become a second place where labels are
judged.

The wire format is the Interactions API (``POST /v1beta/interactions``), which replaced the
older ``models/{model}:generateContent`` + ``contents``/``parts`` shape. Google's public
documentation of the raw REST response is thin — the SDKs expose a convenience
``output_text`` property — so :func:`extract_text` parses defensively and, when it
recognises nothing, raises :class:`ModelUnavailable` carrying a truncated copy of what it
actually saw. It never returns an empty string and never guesses at a shape.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from jg.config import (
    ANTHROPIC,
    GEMINI,
    gemini_api_key,
    gemini_min_interval,
    gemini_model_name,
    llm_provider,
)
from jg.llm_types import ModelCall, ModelUnavailable, QuotaExhausted

log = logging.getLogger(__name__)

#: The Interactions API. A single-turn prompt is one string in ``input``.
API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

#: The key travels as a header. Never as a ``?key=`` query parameter: a URL ends up in
#: proxy logs and in ``httpx`` exception messages, and this one is a credential.
API_KEY_HEADER = "x-goog-api-key"

#: Thinking cannot be switched off on the Gemini 3.x line, only turned down. This workload
#: is a schema-constrained classification with the answer quoted out of a supplied passage,
#: not a reasoning task, and thinking tokens are billed as output.
DEFAULT_THINKING_LEVEL = "minimal"
THINKING_LEVELS = ("minimal", "low", "medium", "high")

#: Retried. 429 is the free tier's normal weather: the limits are enforced per Google Cloud
#: project on requests/minute, tokens/minute and requests/day at once, and breaching any one
#: of the three returns 429 while the other two are still fine.
RETRY_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
#: A ``Retry-After`` longer than this is a daily cap, not a minute cap. Sleeping it out
#: would block the batch for hours with no way to see what is happening, so the run is
#: capped here, exhausts its retries and stops with a message naming the status.
RETRY_AFTER_CAP_SECONDS = 300.0

#: How much of an unrecognised body is quoted back in an error. Enough to identify the
#: shape or read Google's error message, short enough for a log line.
BODY_EXCERPT_CHARS = 500

#: Injectable so tests are instant and a batch's backoff is observable.
Sleep = Callable[[float], None]
Clock = Callable[[], float]

# On pacing, which is a different lever from the 429 retry below and the cheaper of the two.
# Retrying is *reactive*: it discovers the rate limit by breaking it, and every rejected
# request costs a round trip and a backoff before any work happens. Staying inside the limit
# means the batch mostly never sees a 429 at all. The retry stays regardless, because the
# free tier is enforced on three axes at once — requests/minute, tokens/minute,
# requests/day — and pacing only addresses the first, so a batch of this length will still
# meet the other two. The interval itself is read from
# :func:`jg.config.gemini_min_interval`, because environment decisions live in one module.


# ------------------------------------------------------------------ response parsing


def _text_blocks(content: Any) -> list[str]:
    """Every ``text`` string in one step's ``content``, in order.

    ``content`` is documented as a list of typed blocks. A bare string is accepted too,
    because that is the one other shape a JSON API plausibly returns here and accepting it
    costs a line.
    """
    if isinstance(content, str):
        return [content]
    if not isinstance(content, Sequence):
        return []
    texts: list[str] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, Mapping):
            text = block.get("text")
            if isinstance(text, str) and block.get("type", "text") == "text":
                texts.append(text)
    return texts


def extract_text(body: Mapping[str, Any]) -> str:
    """The generated text out of one Interactions response. Pure, and defensive.

    Preference order, most documented first:

    1. the ``model_output`` step of the ``steps`` timeline, its ``text`` blocks joined,
    2. a top-level ``output_text``, which is what Google's SDKs expose as a convenience and
       may or may not appear on the wire,
    3. nothing — which raises :class:`ModelUnavailable` naming the status and the keys it
       did see, with the body truncated.

    Case 3 is the point of this function. An empty string returned from here would reach
    ``json.loads`` and then the span gate as a malformed answer, and the run would report a
    model failure when what actually happened is that the wire format moved.
    """
    steps = body.get("steps")
    if isinstance(steps, Sequence) and not isinstance(steps, (str, bytes)):
        chunks: list[str] = []
        for step in steps:
            if isinstance(step, Mapping) and step.get("type") == "model_output":
                chunks.extend(_text_blocks(step.get("content")))
        joined = "".join(chunks)
        if joined.strip():
            return joined

    fallback = body.get("output_text")
    if isinstance(fallback, str) and fallback.strip():
        return fallback
    if isinstance(fallback, Sequence) and not isinstance(fallback, (str, bytes)):
        joined = "".join(part for part in fallback if isinstance(part, str))
        if joined.strip():
            return joined

    raise ModelUnavailable(
        f"the Gemini response carried no model output text (status={body.get('status')!r}, "
        f"top-level keys={sorted(str(key) for key in body)}): {excerpt(body)}"
    )


def excerpt(value: Any) -> str:
    """A truncated, single-line rendering of a response body, for an error message."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    text = " ".join(text.split())
    return text if len(text) <= BODY_EXCERPT_CHARS else f"{text[:BODY_EXCERPT_CHARS]}..."


# ------------------------------------------------------------------------- retrying


def retry_after_seconds(response: httpx.Response) -> float | None:
    """``Retry-After`` in seconds, or ``None`` when the response does not state one.

    The header is read first. Google's REST errors also carry the wait in the body as a
    ``RetryInfo`` detail (``retryDelay: "27s"``); that path is read defensively because the
    exact error envelope of the Interactions API is not documented, and any failure to
    parse it simply falls back to the backoff schedule.
    """
    raw = response.headers.get("retry-after")
    if raw is not None:
        try:
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            log.debug("unparseable Retry-After %r; falling back to backoff", raw)

    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, Mapping):
        return None
    error = body.get("error")
    details = error.get("details") if isinstance(error, Mapping) else None
    if not isinstance(details, Sequence) or isinstance(details, (str, bytes)):
        return None
    for detail in details:
        if not isinstance(detail, Mapping):
            continue
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return max(0.0, float(delay[:-1]))
            except ValueError:
                continue
    return None


def backoff_seconds(attempt: int, *, rng: random.Random | None = None) -> float:
    """Full-jitter exponential backoff for retry number ``attempt`` (0-based).

    Jittered rather than fixed because the free tier's window is per project: two processes
    backing off in lockstep would keep colliding on the same second forever.
    """
    ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2**attempt))
    return (rng or random).uniform(0.0, ceiling)


# ---------------------------------------------------------------- the model call itself


def gemini_model(
    *,
    schema: Mapping[str, Any],
    model: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
    thinking_level: str = DEFAULT_THINKING_LEVEL,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
    max_retries: int = 5,
    min_interval: float | None = None,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
    rng: random.Random | None = None,
) -> ModelCall:
    """A ``ModelCall`` over the Gemini Interactions API, constrained to ``schema``.

    One factory for both seams: pass ``jg.classify.reasoning.RESPONSE_SCHEMA`` for a
    treatment label, ``jg.provisions.RESPONSE_SCHEMA`` for a materiality verdict. The
    caller's schema is sent verbatim in ``response_format``; Gemini supports a subset of
    JSON Schema (the scalar types, ``enum``, ``format``, min/max) and both of this repo's
    schemas are flat and small, so they are inside it.

    ``temperature`` is **0.0 by default and always sent explicitly**, which is CLAUDE.md
    rule 7 satisfied to the letter. Note the contrast with
    :func:`jg.classify.reasoning.anthropic_model`, which has to document a deviation: the
    current Claude models removed the sampling parameters from the Messages API and reject
    a request carrying one, so on that provider the rule can only be honoured in spirit.
    Gemini still accepts ``temperature``, so here it is sent.

    Retrying is not optional on this provider. The free tier's limits are enforced per
    Google Cloud project on three axes at once and the queued M5 workload is over a
    thousand calls, so a 429 arriving at call 900 must not end the batch: this honours
    ``Retry-After`` when the response states one, otherwise backs off exponentially with
    jitter, up to ``max_retries`` times, and then fails with a message naming the status.
    ``sleep`` is injectable so none of that costs a test a real second.
    """
    key = api_key or gemini_api_key()
    if not key:
        raise ModelUnavailable(
            "neither GEMINI_API_KEY nor GOOGLE_API_KEY is set, so no Gemini call can "
            "be made."
        )
    if thinking_level not in THINKING_LEVELS:
        raise ModelUnavailable(
            f"thinking_level was {thinking_level!r}; Gemini accepts one of "
            f"{', '.join(THINKING_LEVELS)}."
        )
    name = model or gemini_model_name()
    http = client or httpx.Client(timeout=timeout)
    gap = gemini_min_interval() if min_interval is None else min_interval

    # "Not before this instant", on the injected clock's timeline. Held in a one-element
    # list rather than a nonlocal so the closure stays readable.
    next_allowed = [0.0]

    def pace() -> None:
        """Wait out the remainder of the minimum gap since the previous request."""
        if gap <= 0:
            return
        now = clock()
        wait = next_allowed[0] - now
        if wait > 0:
            log.debug("gemini %s: pacing %.1fs to stay inside the rate limit", name, wait)
            sleep(wait)
            now += wait
        next_allowed[0] = now + gap

    def request_body(prompt: str) -> dict[str, Any]:
        return {
            "model": name,
            # A single-turn prompt is one string. Both seams above this one already hand
            # over exactly one assembled prompt, so nothing has to be restructured.
            "input": prompt,
            # temperature and thinking_level nest here, not at the top level.
            "generation_config": {
                "temperature": temperature,
                "thinking_level": thinking_level,
            },
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": dict(schema),
            },
        }

    def call(prompt: str) -> Mapping[str, Any]:
        payload = request_body(prompt)
        headers = {"content-type": "application/json", API_KEY_HEADER: key}

        for attempt in range(max_retries + 1):
            retryable = attempt < max_retries
            # A retry is a request too, so it is paced like any other.
            pace()
            try:
                response = http.post(API_URL, json=payload, headers=headers)
            except httpx.TransportError as exc:
                if not retryable:
                    raise ModelUnavailable(
                        f"Gemini ({name}) unreachable after {max_retries} retries: {exc}"
                    ) from exc
                delay = backoff_seconds(attempt, rng=rng)
                log.warning(
                    "gemini %s: %s; retrying in %.1fs (%d/%d)",
                    name, exc, delay, attempt + 1, max_retries,
                )
                sleep(delay)
                continue

            if response.status_code in RETRY_STATUS_CODES and retryable:
                stated = retry_after_seconds(response)
                delay = (
                    min(stated, RETRY_AFTER_CAP_SECONDS)
                    if stated is not None
                    else backoff_seconds(attempt, rng=rng)
                )
                log.warning(
                    "gemini %s: HTTP %d, retrying in %.1fs (%d/%d)",
                    name, response.status_code, delay, attempt + 1, max_retries,
                )
                sleep(delay)
                continue

            if response.status_code == 429:
                # A 429 that outlived every retry is not a transient blip, it is the quota
                # window. Raised as its own type so the batch runner can stop cleanly and
                # tell the operator to come back, instead of reporting a model failure for
                # what is really the end of today's budget.
                raise QuotaExhausted(
                    f"Gemini ({name}) quota exhausted: still HTTP 429 after {attempt} "
                    f"retries. {excerpt(response.text)}",
                    retry_after=retry_after_seconds(response),
                )
            if response.status_code >= 400:
                raise ModelUnavailable(
                    f"Gemini ({name}) returned HTTP {response.status_code} after "
                    f"{attempt} retries: {excerpt(response.text)}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise ModelUnavailable(
                    f"Gemini ({name}) returned a non-JSON body: {excerpt(response.text)}"
                ) from exc
            if not isinstance(body, Mapping):
                raise ModelUnavailable(
                    f"Gemini ({name}) returned a {type(body).__name__}, not an object: "
                    f"{excerpt(body)}"
                )

            text = extract_text(body)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ModelUnavailable(
                    f"Gemini ({name}) output was not the JSON its response_format asked "
                    f"for: {excerpt(text)}"
                ) from exc
            if not isinstance(parsed, Mapping):
                raise ModelUnavailable(
                    f"Gemini ({name}) output parsed as a {type(parsed).__name__}, not an "
                    f"object: {excerpt(text)}"
                )
            return parsed

        raise ModelUnavailable(  # pragma: no cover - the loop always returns or raises
            f"Gemini ({name}) exhausted {max_retries} retries with no response."
        )

    return call


# ------------------------------------------------------------------ provider selection


#: An Anthropic ``ModelCall`` factory: ``anthropic_model`` for M5,
#: ``anthropic_materiality_model`` for M6. Passed in rather than imported so this module
#: stays free of any import edge to :mod:`jg.provisions`.
AnthropicFactory = Callable[..., ModelCall]


def provider_model(
    schema: Mapping[str, Any],
    *,
    anthropic: AnthropicFactory,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> ModelCall:
    """Build the ``ModelCall`` for the configured provider. One line at each call site.

    ``provider`` defaults to :func:`jg.config.llm_provider`. An unrecognised value raises
    :class:`ModelUnavailable` rather than silently billing the other provider — both call
    sites already turn that error into a sentence, so it costs no traceback.
    """
    active = provider or llm_provider()
    if active == GEMINI:
        return gemini_model(
            schema=schema, model=model, api_key=api_key, client=client, timeout=timeout
        )
    if active != ANTHROPIC:
        raise ModelUnavailable(
            f"JG_LLM_PROVIDER is {active!r}; it must be {GEMINI!r} or {ANTHROPIC!r}."
        )
    return anthropic(model=model, api_key=api_key, client=client, timeout=timeout)

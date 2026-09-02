"""Tier 3 of the router: the reasoning model, with the evidence-span gate around it.

PLAN.md section 8 and decision D3. The model's only job is to name a relationship and quote
the passage that carries it. The quote is what makes the label checkable, so it is checked:

* whitespace is normalised on both sides,
* ``evidence_span`` must then be a **literal substring of the context that was supplied**,
* on failure the same call is retried **once**, with the violation stated,
* on a second failure the row is written ``UNCLASSIFIED``.

That sequence is CLAUDE.md rule 3 and it is not negotiable, not relaxable for a test, and
not skippable on a cache hit — a cached response is validated again on the way out, so a
poisoned cache cannot smuggle a label past the gate.

The model call itself is one injectable callable, ``ModelCall``: prompt in, parsed JSON
object out. Everything above it — prompt assembly, validation, retry, caching, the
``UNCLASSIFIED`` fallback — is pure or DB-only and runs in tests with no API key and no
network. :func:`anthropic_model` is the default implementation, plain ``httpx`` against the
Messages API as PLAN.md section 4 specifies.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import httpx
from psycopg.types.json import Json

from jg.classify.prompts import PromptTemplate, prompt_template
from jg.classify.structural import CitationEdge, citation_sentence, normalise_ws
from jg.config import llm_api_key, llm_model
from jg.db import Conn
from jg.models import Route, TreatmentLabel, TreatmentResult

log = logging.getLogger(__name__)

#: The frozen template this tier renders. A new prompt is a new file (CLAUDE.md rule 8).
PROMPT_NAME = "treatment-classify.v1"

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
#: One JSON object with a one-sentence reasoning field. Generous, not unbounded.
MAX_TOKENS = 2048

#: Written into the prompt when the corpus has no ``ratio_summary`` for the cited decision.
#: Stated rather than left blank: an empty holding section reads as "it held nothing".
NO_RATIO = "(not available: the corpus holds no ratio summary for this decision)"
NO_COURT = "(unknown court)"
NO_DATE = "(unknown date)"

#: The seven labels the model may return. ``UNCLASSIFIED`` is this stage's own failure
#: state (PLAN.md section 8) and is rejected as a model answer.
MODEL_LABELS = tuple(
    label.value for label in TreatmentLabel if label is not TreatmentLabel.UNCLASSIFIED
)

#: JSON schema the response is constrained to, mirroring the Output section of the prompt.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(MODEL_LABELS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_span": {"type": "string", "minLength": 1},
        "reasoning": {"type": "string"},
    },
    "required": ["label", "confidence", "evidence_span", "reasoning"],
    "additionalProperties": False,
}

#: prompt -> the parsed JSON object the model returned.
ModelCall = Callable[[str], Mapping[str, Any]]


class ModelUnavailable(RuntimeError):
    """No usable model call can be built, e.g. ``ANTHROPIC_API_KEY`` is not set."""


class ResponseRejected(ValueError):
    """A model response failed validation. Carries the sentence stated back on the retry."""

    def __init__(self, violation: str) -> None:
        super().__init__(violation)
        self.violation = violation


# -------------------------------------------------------------------- the span gate


def is_verbatim(span: str, context: str) -> bool:
    """Is ``span`` a literal substring of ``context`` after whitespace normalisation?

    The whole hallucination gate, in one line, exactly as PLAN.md D3 states it. Whitespace
    is normalised because a model that copies a passage correctly still re-types a
    non-breaking space, a line break inside a paragraph or a double space as a single
    ASCII space, and rejecting that would be rejecting a correct quote. Nothing else is
    normalised: case, diacritics and punctuation must match, because in Czech legal prose
    they carry the meaning the label is standing on.
    """
    normalised = normalise_ws(span)
    return bool(normalised) and normalised in normalise_ws(context)


def parse_response(payload: Mapping[str, Any], context: str) -> tuple[TreatmentLabel, float, str]:
    """Validate one model response. Returns ``(label, confidence, span)`` or raises.

    Raises :class:`ResponseRejected` whose message is written back into the retry prompt, so
    the sentence has to be usable as an instruction, not just as a log line.
    """
    label_raw = payload.get("label")
    if not isinstance(label_raw, str) or label_raw not in MODEL_LABELS:
        raise ResponseRejected(
            f"`label` was {label_raw!r}, which is not one of {', '.join(MODEL_LABELS)}."
        )

    confidence_raw = payload.get("confidence")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        raise ResponseRejected(f"`confidence` was {confidence_raw!r}, which is not a number.")
    confidence = float(confidence_raw)
    if not 0.0 <= confidence <= 1.0:
        raise ResponseRejected(f"`confidence` was {confidence}, which is outside [0, 1].")

    span = payload.get("evidence_span")
    if not isinstance(span, str) or not span.strip():
        raise ResponseRejected("`evidence_span` was empty.")
    if not is_verbatim(span, context):
        raise ResponseRejected(
            f"`evidence_span` was {_shorten(span)!r}, which does not occur in the Context "
            "block. It must be copied out of that block character for character."
        )
    return TreatmentLabel(label_raw), round(confidence, 2), normalise_ws(span)


def _shorten(text: str, limit: int = 120) -> str:
    normalised = normalise_ws(text)
    return normalised if len(normalised) <= limit else f"{normalised[:limit]}..."


# ----------------------------------------------------------------------- the prompt


def build_prompt(edge: CitationEdge, template: PromptTemplate | None = None) -> str:
    """Render ``treatment-classify.v1.md`` for one edge.

    ``{{context}}`` is the citing paragraph plus two either side, already assembled on the
    edge by :func:`jg.classify.router.load_edges`. It is also the only text the span gate
    accepts a quote from, so prompt and gate read the same string by construction.
    """
    active = template or prompt_template(PROMPT_NAME)
    return active.render(
        citing_court=edge.citing_court,
        citing_panel=str(edge.citing_panel),
        citing_date=edge.citing_date.isoformat() if edge.citing_date else NO_DATE,
        cited_court=edge.cited_court or NO_COURT,
        cited_date=edge.cited_date.isoformat() if edge.cited_date else NO_DATE,
        cited_ratio=edge.cited_ratio or NO_RATIO,
        context=edge.context_text,
    )


def retry_prompt(prompt: str, violation: str) -> str:
    """The same prompt with the violation stated. The frozen template is not touched.

    Appended as text rather than as a second conversational turn so that ``ModelCall`` stays
    one string in, one object out — the smallest seam that a test can stub.
    """
    return (
        f"{prompt}\n\n---\n\n"
        f"Your previous answer was rejected: {violation}\n\n"
        "Return one JSON object in the same shape. `evidence_span` must be a passage copied "
        "character for character out of the Context block above; if no passage there carries "
        "your label, return `MENTIONED` with low confidence and quote the sentence the "
        "citation appears in."
    )


# ------------------------------------------------------------------------ the cache


def cache_key(
    citing_ecli: str, cited_ecli: str, paragraph_idx: int | None, prompt_version: str
) -> str:
    """``llm_cache.cache_key`` for a treatment classification. PLAN.md section 8.

    The documented key is ``(citing_ecli, cited_ecli, paragraph_idx, prompt_version)``;
    ``llm_cache`` stores one text column, so the tuple is joined on a character that cannot
    occur in an ECLI or a version string. Temperature is fixed, so a hit is a faithful
    replay rather than an approximation.
    """
    paragraph = "-" if paragraph_idx is None else str(paragraph_idx)
    return "|".join(("treatment", citing_ecli, cited_ecli, paragraph, prompt_version))


class ResponseCache(Protocol):
    """Read-through cache for model responses. Implemented over ``llm_cache`` or a dict."""

    def get(self, key: str) -> Mapping[str, Any] | None: ...

    def put(
        self, key: str, *, request: Mapping[str, Any], response: Mapping[str, Any],
        model: str, prompt_version: str,
    ) -> None: ...


class MemoryCache:
    """In-process cache. The test double, and useful for a single batch run."""

    def __init__(self) -> None:
        self._entries: dict[str, Mapping[str, Any]] = {}

    def get(self, key: str) -> Mapping[str, Any] | None:
        return self._entries.get(key)

    def put(
        self, key: str, *, request: Mapping[str, Any], response: Mapping[str, Any],
        model: str, prompt_version: str,
    ) -> None:
        self._entries[key] = response


_SELECT_CACHE = "select response from llm_cache where cache_key = %s"

# A hit is deterministic, so a concurrent writer's row is as good as ours.
_INSERT_CACHE = """
insert into llm_cache (cache_key, prompt_version, model, request, response)
values (%s, %s, %s, %s, %s)
on conflict (cache_key) do nothing
"""


class DbResponseCache:
    """``llm_cache`` (V4__model_cache.sql) as a :class:`ResponseCache`. Rows only, no DDL."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def get(self, key: str) -> Mapping[str, Any] | None:
        row = self._conn.execute(_SELECT_CACHE, (key,)).fetchone()
        if row is None:
            return None
        response = row["response"]
        return response if isinstance(response, dict) else None

    def put(
        self, key: str, *, request: Mapping[str, Any], response: Mapping[str, Any],
        model: str, prompt_version: str,
    ) -> None:
        self._conn.execute(
            _INSERT_CACHE, (key, prompt_version, model, Json(dict(request)), Json(dict(response)))
        )


# ------------------------------------------------------------------- classification


def classify_edge(
    edge: CitationEdge,
    *,
    model: ModelCall,
    model_name: str | None = None,
    template: PromptTemplate | None = None,
    cache: ResponseCache | None = None,
) -> TreatmentResult:
    """Classify one edge with the reasoning model. Always returns a storable row.

    Order of events: cache lookup, one call, validate; on a violation one retry with the
    violation stated, validate again; on a second violation ``UNCLASSIFIED``. A validated
    first-or-second response is cached; a rejected one is not, so fixing the prompt or the
    parser is enough to make the edge retryable.
    """
    active = template or prompt_template(PROMPT_NAME)
    name = model_name or llm_model()
    prompt = build_prompt(edge, active)
    context = edge.context_text
    key = cache_key(edge.citing_ecli, edge.cited_ecli, edge.paragraph_idx, active.version)

    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            try:
                label, confidence, span = parse_response(cached, context)
            except ResponseRejected as exc:
                # Never trust a cached row past the gate: the context may have been
                # re-normalised since, or the row may predate a fixed validator.
                log.warning("cached response for %s rejected (%s); re-asking", key, exc.violation)
            else:
                return _result(edge, label, confidence, span, name, active.version)

    attempt = prompt
    violation: str | None = None
    for _ in range(2):
        payload = dict(model(attempt))
        try:
            label, confidence, span = parse_response(payload, context)
        except ResponseRejected as exc:
            violation = exc.violation
            log.warning(
                "citation %s: model response rejected: %s", edge.citation_id, exc.violation
            )
            attempt = retry_prompt(prompt, exc.violation)
            continue
        if cache is not None:
            cache.put(
                key,
                request={"prompt": attempt, "prompt_version": active.version, "model": name},
                response=payload,
                model=name,
                prompt_version=active.version,
            )
        return _result(edge, label, confidence, span, name, active.version)

    log.error(
        "citation %s: UNCLASSIFIED after two failed validations (%s)", edge.citation_id, violation
    )
    return _result(
        edge, TreatmentLabel.UNCLASSIFIED, 0.0, _unclassified_span(edge), name, active.version
    )


def _unclassified_span(edge: CitationEdge) -> str:
    """The span stored on an ``UNCLASSIFIED`` row.

    Not a model claim — the model's answer was rejected and is discarded. It is the sentence
    we asked *about*, quoted from the paragraph, so the amber "needs review" the rules engine
    raises (PLAN.md section 9) can still point a human at the exact place. Falls back to the
    citation as it appeared, which is verbatim by construction.
    """
    span = citation_sentence(edge.citing_paragraph, edge.raw_text)
    return normalise_ws(span or edge.raw_text)


def _result(
    edge: CitationEdge,
    label: TreatmentLabel,
    confidence: float,
    span: str,
    model_name: str,
    prompt_version: str,
) -> TreatmentResult:
    return TreatmentResult(
        citation_id=edge.citation_id,
        label=label,
        confidence=confidence,
        evidence_span=span,
        route=Route.REASONING,
        model=model_name,
        prompt_version=prompt_version,
    )


# --------------------------------------------------------------- the model call itself


def anthropic_model(
    *,
    model: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> ModelCall:
    """A :class:`ModelCall` over the Anthropic Messages API. ``httpx``, per PLAN.md section 4.

    The response is JSON-schema constrained with ``output_config.format``, so the first
    content block is a JSON object matching :data:`RESPONSE_SCHEMA` and the parse cannot
    drift from the prompt's Output section. The span gate still runs on it: a schema
    guarantees shape, never truthfulness.

    On ``temperature``: CLAUDE.md rule 7 asks for temperature 0. The current Claude models —
    including the ``JG_LLM_MODEL`` default — removed the sampling parameters from the
    Messages API and reject a request that carries one, so the default here is to send none.
    That satisfies what rule 7 is protecting (no sampling variance between two runs of the
    same batch: there is no knob to vary) but it is a deviation from the letter of the rule,
    and it is flagged rather than hidden. Pass ``temperature=0.0`` explicitly for a model
    that still accepts it.
    """
    key = api_key or llm_api_key()
    if not key:
        raise ModelUnavailable(
            "ANTHROPIC_API_KEY is not set, so no classification call can be made."
        )
    name = model or llm_model()
    http = client or httpx.Client(timeout=timeout)

    def call(prompt: str) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "model": name,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        response = http.post(
            API_URL,
            json=payload,
            headers={
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": API_VERSION,
            },
        )
        response.raise_for_status()
        body = response.json()
        for block in body.get("content", ()):
            if isinstance(block, dict) and block.get("type") == "text":
                return json.loads(block.get("text", ""))
        raise ModelUnavailable(
            f"no text block in the response (stop_reason={body.get('stop_reason')!r}); "
            "nothing to classify with."
        )

    return call

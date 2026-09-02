"""Treatment classification: three tiers, cheapest first. PLAN.md section 8, M3 and M5.

An edge is a citation from one decision to another. This stage decides what the citing
decision *does* to the cited one, and it is organised around one economic fact: the
reasoning model costs money per edge and the corpus has tens of thousands of them.

============================  =====================================================
:mod:`jg.classify.structural`  Tier 1. Pure functions over text plus metadata:
                               party-submission ``MENTIONED``, výrok-based ``QUASHED``.
                               No model, no network, no database.
:mod:`jg.classify.router`      The tier decision itself, plus the batch run. Escalates
                               extended/grand/plenary panels and departure markers,
                               triages the rest, escalates below confidence 0.75.
:mod:`jg.classify.reasoning`   Tier 3. The prompt, the model call, and the
                               evidence-span gate that decides whether its answer may
                               be stored at all (CLAUDE.md rule 3).
:mod:`jg.classify.prompts`     ``prompts/*.md``: frontmatter, version, placeholders.
============================  =====================================================

The evidence layer stops here. These labels are facts about pairs of documents; turning
them into a traffic light is the Java rules engine's job and no verdict logic belongs in
this package (PLAN.md D2).
"""

from __future__ import annotations

from jg.classify.prompts import PromptTemplate, load_prompt, prompt_template
from jg.classify.reasoning import (
    DbResponseCache,
    MemoryCache,
    ModelCall,
    ResponseRejected,
    anthropic_model,
    build_prompt,
    cache_key,
    classify_edge,
    is_verbatim,
)
from jg.classify.router import (
    Escalation,
    EscalationReason,
    RouterStats,
    TriageVerdict,
    context_window,
    default_triage,
    dry_run,
    load_edges,
    route,
    route_all,
    run_classify,
)
from jg.classify.structural import (
    CitationEdge,
    MarkerSet,
    citation_sentence,
    classify_structural,
    departure_span,
    load_markers,
    marker_set,
    party_submission_span,
    quashed_span,
)

__all__ = [
    "CitationEdge",
    "DbResponseCache",
    "Escalation",
    "EscalationReason",
    "MarkerSet",
    "MemoryCache",
    "ModelCall",
    "PromptTemplate",
    "ResponseRejected",
    "RouterStats",
    "TriageVerdict",
    "anthropic_model",
    "build_prompt",
    "cache_key",
    "citation_sentence",
    "classify_edge",
    "classify_structural",
    "context_window",
    "default_triage",
    "departure_span",
    "dry_run",
    "is_verbatim",
    "load_edges",
    "load_markers",
    "load_prompt",
    "marker_set",
    "party_submission_span",
    "prompt_template",
    "quashed_span",
    "route",
    "route_all",
    "run_classify",
]

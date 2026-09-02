"""Parsed shapes shared across pipeline stages.

These are the seams between stages. `crawl` produces `RawDecision`; `normalize` turns it
into rows; `extract` produces `Reference`; `classify` produces `TreatmentResult`.
Keep them stable — changing one is a cross-stage change.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field


class PanelType(StrEnum):
    """`decision.panel_type`. See PLAN.md section 1 for what each panel can do."""

    PANEL = "panel"
    EXTENDED = "extended"  # rozšířený senát (NSS)
    GRAND = "grand"  # velký senát (NS)
    PLENARY = "plenary"  # plénum (ÚS)
    UNKNOWN = "unknown"


class AliasKind(StrEnum):
    """`decision_alias.alias_kind`."""

    CASE_NO = "case_no"
    REF_NO = "ref_no"
    JOURNAL_NO = "journal_no"
    ECLI_VARIANT = "ecli_variant"


class ReferenceKind(StrEnum):
    """What a regex in extract/patterns.toml matched."""

    REF_NO = "ref_no"
    CASE_NO = "case_no"
    ECLI = "ecli"
    JOURNAL_NO = "journal_no"
    PROVISION = "provision"


class TreatmentLabel(StrEnum):
    """The seven labels plus the failure state. PLAN.md section 8.

    Mirrors ``tech.judikatguard.status.TreatmentLabel`` on the Java side; the names are the
    values stored in ``treatment.label``, so the two must not drift.
    """

    FOLLOWED = "FOLLOWED"
    MENTIONED = "MENTIONED"
    DISTINGUISHED = "DISTINGUISHED"
    NARROWED = "NARROWED"
    DEPARTED = "DEPARTED"
    CRITICIZED = "CRITICIZED"
    QUASHED = "QUASHED"
    UNCLASSIFIED = "UNCLASSIFIED"


class Route(StrEnum):
    """`treatment.route`. Which router tier produced the label. PLAN.md section 8."""

    STRUCTURAL = "structural"
    TRIAGE = "triage"
    REASONING = "reasoning"


class RawDecision(BaseModel):
    """One decision as scraped, before normalisation. The crawl -> normalize seam."""

    ecli: str
    court_code: str
    panel_type: PanelType = PanelType.UNKNOWN
    decided_on: dt.date
    case_no: str | None = None
    ref_no: str | None = None
    journal_no: str | None = None
    ratio_summary: str | None = None
    source_url: str
    fetched_at: dt.datetime
    #: Full decision text, already split into paragraphs, 1-based index by position.
    paragraphs: list[str] = Field(default_factory=list)
    #: The operative part, kept separate because the QUASHED rule reads only this.
    verdict_text: str | None = None


class Reference(BaseModel):
    """One citation-like span found in a document. The extract -> resolve seam."""

    kind: ReferenceKind
    raw_text: str
    #: Character offsets into the paragraph body, not the whole document.
    start: int
    end: int
    paragraph_idx: int
    #: Named capture groups from extract/patterns.toml, e.g. {"section": "2000"}.
    groups: dict[str, str] = Field(default_factory=dict)
    #: Filled by the resolver. None means "not in corpus", which is a coverage metric.
    resolved_ecli: str | None = None
    resolved_provision_id: int | None = None
    #: How the resolver arrived at the act number for an anaphoric provision reference.
    resolution_basis: str | None = None


class TreatmentResult(BaseModel):
    """One classified citation edge. The classify -> db seam."""

    citation_id: int
    label: TreatmentLabel
    confidence: float
    evidence_span: str
    route: Route
    model: str | None = None
    prompt_version: str

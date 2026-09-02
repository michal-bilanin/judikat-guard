"""Citation extraction: the rules pass, resolution, and the run that writes ``citation`` rows.

PLAN.md section 7. The ceiling on everything downstream, so the coverage numbers this stage
reports are the numbers that cap the whole system.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from jg.db import Conn
from jg.extract.anaphora import candidate_sentences
from jg.extract.patterns import PatternSet, find_references, load_patterns, pattern_set
from jg.extract.resolver import (
    ResolutionStats,
    normalize_alias,
    provision_key,
    resolve_decision,
    resolve_decisions,
    resolve_provisions,
    resolve_references,
    summarise,
)
from jg.models import Reference, ReferenceKind

__all__ = [
    "ExtractStats",
    "PatternSet",
    "ResolutionStats",
    "extract_paragraphs",
    "find_references",
    "load_patterns",
    "normalize_alias",
    "pattern_set",
    "resolve_decision",
    "resolve_decisions",
    "resolve_provisions",
    "resolve_references",
    "run_extract",
    "summarise",
]

log = logging.getLogger(__name__)

#: ``citation.extractor``. The model pass writes ``llm``; this one writes ``rules``.
EXTRACTOR = "rules"

_SELECT_DECISIONS = """
select ecli
  from decision
 where (%s::text is null or court_code = %s)
 order by ecli
"""

_SELECT_PARAGRAPHS = """
select idx, body
  from decision_paragraph
 where ecli = %s
 order by idx
"""

_FIND_PROVISION = """
select id
  from provision
 where act_no = %s and section = %s and subsec is not distinct from %s
"""

# `provision` is unique on (act_no, section, subsec), but Postgres treats NULL subsecs as
# distinct, so `on conflict do nothing` would let duplicates through. Look first, insert
# only on a miss. Batch pipeline, single writer: no race to worry about.
_INSERT_PROVISION = """
insert into provision (act_no, section, subsec)
values (%s, %s, %s)
returning id
"""

# `citation` has no natural unique key, so idempotency is a guard rather than an upsert.
# Two identical citations in one paragraph therefore collapse to one row; the paragraph is
# the finest granularity the schema records.
_INSERT_CITATION = """
insert into citation (citing_ecli, cited_ecli, cited_provision, paragraph_idx, raw_text, extractor)
select %s, %s, %s, %s, %s, %s
 where not exists (
   select 1
     from citation c
    where c.citing_ecli = %s
      and c.extractor = %s
      and c.paragraph_idx is not distinct from %s
      and c.raw_text = %s
      and c.cited_ecli is not distinct from %s
      and c.cited_provision is not distinct from %s
 )
"""


@dataclass
class ExtractStats:
    """What one ``jg extract`` run did. Printed by the CLI, consumed by ``make eval-extract``."""

    decisions: int = 0
    paragraphs: int = 0
    references: int = 0
    citations_written: int = 0
    provisions_created: int = 0
    anaphora_candidates: int = 0
    resolution: ResolutionStats = field(default_factory=ResolutionStats)


def extract_paragraphs(
    paragraphs: Sequence[str], patterns: PatternSet | None = None
) -> list[Reference]:
    """Run the rules pass over a whole document. ``paragraph_idx`` is 1-based."""
    refs: list[Reference] = []
    for paragraph_idx, body in enumerate(paragraphs, start=1):
        refs.extend(find_references(body, paragraph_idx, patterns))
    return refs


def _provision_id(conn: Conn, key: tuple[str, str, str | None]) -> tuple[int, bool]:
    """Provision id for ``(act_no, section, subsec)``, creating the row if it is new.

    The identity row is created from what the document actually says; the *text* of the
    provision and its validity dates come from the e-Sbírka ingest (PLAN.md section 10) and
    are never guessed here.
    """
    row = conn.execute(_FIND_PROVISION, key).fetchone()
    if row is not None:
        return int(row["id"]), False
    created = conn.execute(_INSERT_PROVISION, key).fetchone()
    if created is None:  # pragma: no cover - `returning id` always yields a row
        raise RuntimeError(f"insert into provision returned no id for {key}")
    return int(created["id"]), True


def _write_citations(citing_ecli: str, refs: Sequence[Reference], conn: Conn) -> tuple[int, int]:
    """Persist resolved references as ``citation`` rows. Returns (written, provisions created).

    Unresolved references are *not* written. ``citation`` carries a check constraint
    requiring at least one of ``cited_ecli`` / ``cited_provision``, so a reference that
    resolved to neither has no representable row. It is counted in
    :class:`~jg.extract.resolver.ResolutionStats` instead, which is where the coverage
    metric PLAN.md section 7 asks for lives.
    """
    written = 0
    created = 0
    for ref in refs:
        cited_ecli = ref.resolved_ecli
        cited_provision = ref.resolved_provision_id
        if cited_provision is None and ref.kind is ReferenceKind.PROVISION:
            key = provision_key(ref)
            if key is not None:
                cited_provision, is_new = _provision_id(conn, key)
                ref.resolved_provision_id = cited_provision
                created += int(is_new)
        if cited_ecli is None and cited_provision is None:
            continue
        cursor = conn.execute(
            _INSERT_CITATION,
            (
                citing_ecli,
                cited_ecli,
                cited_provision,
                ref.paragraph_idx,
                ref.raw_text,
                EXTRACTOR,
                citing_ecli,
                EXTRACTOR,
                ref.paragraph_idx,
                ref.raw_text,
                cited_ecli,
                cited_provision,
            ),
        )
        written += cursor.rowcount
    return written, created


def run_extract(conn: Conn, court: str | None = None) -> ExtractStats:
    """Extract, resolve and persist citations for every decision in the corpus.

    Idempotent: a second run over unchanged text writes no new rows. No model call is made;
    the anaphora pass is only counted here so the CLI can report how much the rules pass
    leaves on the table.
    """
    patterns = pattern_set()
    stats = ExtractStats()

    eclis = [
        row["ecli"] for row in conn.execute(_SELECT_DECISIONS, (court, court)).fetchall()
    ]
    for citing_ecli in eclis:
        rows = conn.execute(_SELECT_PARAGRAPHS, (citing_ecli,)).fetchall()
        if not rows:
            continue
        # decision_paragraph.idx may have gaps; rebuild a dense 1-based list so the
        # extractor's own indices stay in step with the stored ones.
        highest = max(int(row["idx"]) for row in rows)
        paragraphs = [""] * highest
        for row in rows:
            paragraphs[int(row["idx"]) - 1] = row["body"]

        refs = extract_paragraphs(paragraphs, patterns)
        resolve_references(refs, conn)
        written, created = _write_citations(citing_ecli, refs, conn)
        # Coverage is measured *after* the write: a provision reference that carried an act
        # number resolves to a provision row this run may itself have created, and counting
        # it as unresolved would understate the metric.
        resolution = summarise(refs)

        stats.decisions += 1
        stats.paragraphs += len(rows)
        stats.references += len(refs)
        stats.citations_written += written
        stats.provisions_created += created
        stats.anaphora_candidates += len(candidate_sentences(paragraphs, refs, patterns))
        stats.resolution.merge(resolution)

    log.info(
        "extract: %d decisions, %d references, %d citations, coverage %.3f",
        stats.decisions,
        stats.references,
        stats.citations_written,
        stats.resolution.coverage,
    )
    return stats

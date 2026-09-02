"""RawDecision -> database rows.

The crawl -> database seam. Writes ``decision``, ``decision_alias``,
``decision_paragraph`` and refreshes ``corpus_meta``. Every write is an upsert, so
re-running a crawl over an already-loaded decision changes nothing.

Flyway owns the schema (CLAUDE.md rule 4): rows only, never DDL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from jg.db import Conn
from jg.extract.resolver import normalize_alias
from jg.models import AliasKind, RawDecision

log = logging.getLogger(__name__)

_UPSERT_DECISION = """
insert into decision (
    ecli, court_code, panel_type, decided_on, case_no, ref_no, journal_no,
    ratio_summary, source_url, fetched_at
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (ecli) do update set
    court_code    = excluded.court_code,
    panel_type    = excluded.panel_type,
    decided_on    = excluded.decided_on,
    case_no       = excluded.case_no,
    ref_no        = excluded.ref_no,
    journal_no    = excluded.journal_no,
    ratio_summary = coalesce(excluded.ratio_summary, decision.ratio_summary),
    source_url    = excluded.source_url,
    fetched_at    = excluded.fetched_at
"""

_UPSERT_ALIAS = """
insert into decision_alias (alias, alias_kind, ecli)
values (%s, %s, %s)
on conflict (alias) do nothing
"""

_UPSERT_PARAGRAPH = """
insert into decision_paragraph (ecli, idx, body)
values (%s, %s, %s)
on conflict (ecli, idx) do update set body = excluded.body
"""

_TRIM_PARAGRAPHS = "delete from decision_paragraph where ecli = %s and idx > %s"

_REFRESH_CORPUS_META = """
insert into corpus_meta (court_code, decision_count, covered_through, refreshed_at)
select d.court_code, count(*), max(d.decided_on), now()
  from decision d
 group by d.court_code
on conflict (court_code) do update set
    decision_count  = excluded.decision_count,
    covered_through = excluded.covered_through,
    refreshed_at    = excluded.refreshed_at
"""


def alias_rows(raw: RawDecision) -> list[tuple[str, str, str]]:
    """``(alias, alias_kind, ecli)`` rows for one decision. Pure; no database access.

    One row per non-null of ``case_no`` / ``ref_no`` / ``journal_no``, plus the ECLI itself
    as an ``ecli_variant``. Aliases are stored through
    :func:`jg.extract.resolver.normalize_alias` so the resolver's lookups match by
    construction.
    """
    pairs: list[tuple[str | None, AliasKind]] = [
        (raw.ecli, AliasKind.ECLI_VARIANT),
        (raw.case_no, AliasKind.CASE_NO),
        (raw.ref_no, AliasKind.REF_NO),
        (raw.journal_no, AliasKind.JOURNAL_NO),
    ]
    rows: dict[str, tuple[str, str, str]] = {}
    for value, kind in pairs:
        if not value:
            continue
        alias = normalize_alias(value)
        if alias:
            rows.setdefault(alias, (alias, str(kind), raw.ecli))
    return list(rows.values())


def paragraph_rows(raw: RawDecision) -> list[tuple[str, int, str]]:
    """``(ecli, idx, body)`` rows. ``idx`` is 1-based and follows ``RawDecision.paragraphs``.

    Blank paragraphs are dropped *before* numbering would be affected — they are not: the
    index is the position in the original list, so dropping a blank leaves a gap rather than
    renumbering the ones after it. Gaps are harmless; renumbering would break every stored
    ``citation.paragraph_idx``.
    """
    return [
        (raw.ecli, idx, body)
        for idx, body in enumerate(raw.paragraphs, start=1)
        if body.strip()
    ]


def store_decision(raw: RawDecision, conn: Conn) -> None:
    """Upsert one decision and everything hanging off it. Idempotent.

    ``RawDecision.verdict_text`` has no column in the schema; the structural classifier
    reads it from the crawl cache, so it is deliberately not persisted here.
    """
    conn.execute(
        _UPSERT_DECISION,
        (
            raw.ecli,
            raw.court_code,
            str(raw.panel_type),
            raw.decided_on,
            raw.case_no,
            raw.ref_no,
            raw.journal_no,
            raw.ratio_summary,
            raw.source_url,
            raw.fetched_at,
        ),
    )

    aliases = alias_rows(raw)
    paragraphs = paragraph_rows(raw)
    with conn.cursor() as cur:
        if aliases:
            cur.executemany(_UPSERT_ALIAS, aliases)
        if paragraphs:
            cur.executemany(_UPSERT_PARAGRAPH, paragraphs)
    # A re-crawl that yields fewer paragraphs must not leave the tail of the old text behind.
    conn.execute(_TRIM_PARAGRAPHS, (raw.ecli, len(raw.paragraphs)))

    log.debug(
        "stored %s: %d paragraphs, %d aliases", raw.ecli, len(paragraphs), len(aliases)
    )


def store_decisions(raws: Iterable[RawDecision], conn: Conn, *, refresh_meta: bool = True) -> int:
    """Upsert many decisions, then refresh ``corpus_meta``. Returns the count stored."""
    stored = 0
    for raw in raws:
        store_decision(raw, conn)
        stored += 1
    if refresh_meta:
        refresh_corpus_meta(conn)
    return stored


def refresh_corpus_meta(conn: Conn) -> Sequence[dict[str, object]]:
    """Recompute per-court counts and coverage dates from ``decision``.

    ``corpus_meta`` is what the UI quotes in "no adverse treatment found in N decisions
    through date C" (PLAN.md section 2), so it is derived, never hand-maintained.
    """
    conn.execute(_REFRESH_CORPUS_META)
    return conn.execute(
        """
        select court_code, decision_count, covered_through, refreshed_at
          from corpus_meta
         order by court_code
        """
    ).fetchall()

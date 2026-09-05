"""Provision ingest and the materiality call. PLAN.md section 10, milestone M6.

Two jobs, kept apart:

**Ingest.** Turn :class:`~jg.crawl.esbirka.ProvisionVersionRecord` values into ``provision``
and ``provision_version`` rows, idempotently. "Idempotent" here is stronger than "does not
crash on a second run": running the same dump twice must leave the *same row ids*, because
``provision_materiality`` is keyed on ``provision_version.id`` and a delete-and-reinsert
would either break that foreign key or orphan a paid-for model verdict.

**Materiality.** A rewording is only an amber if it could have changed the reasoning. That
judgement is one model call against the frozen ``prompts/provision-materiality.v1.md``, and
it is subject to CLAUDE.md rule 3 without exception: the ``evidence_span`` must be a literal
substring of one of the two wordings after whitespace normalisation, one retry is allowed,
and a response that fails twice is **not stored**. There is no ``UNCLASSIFIED`` escape hatch
in ``provision_materiality`` — the table has no such column — so the row is simply absent,
and ``ProvisionRepository`` already reads an absent row as ``material = false``. An unjudged
rewording therefore produces no amber, which is the right way round: CLAUDE.md rule 2 forbids
a light with no evidence behind it.

Flyway owns the schema (CLAUDE.md rule 4). Nothing here issues DDL. Every statement lists
its columns explicitly; there is no ``select *``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from psycopg.types.json import Json

from jg.classify.prompts import PromptTemplate, prompt_template
from jg.classify.reasoning import (
    API_URL,
    API_VERSION,
    ModelUnavailable,
    ResponseRejected,
    is_verbatim,
)
from jg.config import llm_api_key, llm_model, llm_model_name
from jg.crawl.base import Fetcher, normalise_ws
from jg.crawl.esbirka import (
    LOCAL_FORMAT,
    SOURCE_CODE,
    ProvisionRecord,
    ProvisionVersionRecord,
    build_timeline,
    fetch_provision_versions,
    group_by_provision,
    iter_dump_files,
    iter_dump_records,
    parse_local_dump,
)
from jg.db import Conn
from jg.gemini import provider_model

log = logging.getLogger(__name__)

#: The frozen template the materiality verdict renders. A new prompt is a new file
#: (CLAUDE.md rule 8); rows in ``provision_materiality`` reference this version string.
PROMPT_NAME = "provision-materiality.v1"

#: One JSON object with a one-sentence reasoning field.
MAX_TOKENS = 1024

#: Mirrors the Output section of ``provision-materiality.v1.md``. A schema guarantees
#: shape, never truthfulness — the span gate below still runs on every response.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "material": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_span": {"type": "string", "minLength": 1},
        "reasoning": {"type": "string"},
    },
    "required": ["material", "confidence", "evidence_span", "reasoning"],
    "additionalProperties": False,
}

#: prompt -> the parsed JSON object the model returned. Defaults to ``None`` everywhere it
#: is accepted, so the whole module runs in tests with no API key and no network.
MaterialityCall = Callable[[str], Mapping[str, Any]]


class IngestError(RuntimeError):
    """A record cannot be written as a row without inventing something."""


# ------------------------------------------------------------------- SQL fragments

# The Java side's ProvisionRepository.VERSION_IN_FORCE, written once here for the same
# reason it is written once there: it is asked twice per rewording, at decided_on and at
# asOf, and the two must be the same rule. `valid_to >= date` is inclusive, which is why
# build_timeline closes a window on the day *before* the next one opens.
_VERSION_IN_FORCE = """
select pv.id, pv.body, pv.valid_from
  from provision_version pv
 where pv.provision_id = {provision}
   and pv.valid_from <= {date}
   and (pv.valid_to is null or pv.valid_to >= {date})
 order by pv.valid_from desc
 limit 1
"""

# `provision` is unique on (act_no, section, subsec), but Postgres treats NULL subsecs as
# distinct, so `on conflict do nothing` lets duplicates through for every provision cited
# without a subsection — which is most of them. Look first, insert only on a miss. Same
# approach as jg.extract, deliberately: two writers with different idempotency rules on one
# table is how a duplicate identity row appears months later.
_FIND_PROVISION = """
select id
  from provision
 where act_no = %s
   and section = %s
   and subsec is not distinct from %s
"""

_INSERT_PROVISION = """
insert into provision (act_no, section, subsec)
values (%s, %s, %s)
returning id
"""

_SELECT_VERSIONS = """
select id, body, valid_from, valid_to, derogated_by
  from provision_version
 where provision_id = %s
 order by valid_from
"""

_INSERT_VERSION = """
insert into provision_version (provision_id, body, valid_from, valid_to, derogated_by)
values (%s, %s, %s, %s, %s)
returning id
"""

_UPDATE_VERSION = """
update provision_version
   set body = %s,
       valid_to = %s,
       derogated_by = %s
 where id = %s
"""

_DELETE_VERSION = "delete from provision_version where id = %s"

_REFERENCED_VERSIONS = """
select distinct v.id
  from provision_version v
  join provision_materiality m
    on m.from_version_id = v.id
    or m.to_version_id = v.id
 where v.id = any(%s)
"""

_VERSION_AT = """
select id, body, valid_from, valid_to, derogated_by
  from provision_version
 where provision_id = %s
   and valid_from <= %s
   and (valid_to is null or valid_to >= %s)
 order by valid_from desc
 limit 1
"""

# Overlapping windows are the one defect that makes VERSION_IN_FORCE return the wrong
# wording outright rather than merely no wording, so they get their own check.
_OVERLAPS = """
select a.id as earlier_id, a.valid_from as earlier_from, a.valid_to as earlier_to,
       b.id as later_id,   b.valid_from as later_from
  from provision_version a
  join provision_version b
    on b.provision_id = a.provision_id
   and b.valid_from > a.valid_from
 where a.provision_id = %s
   and (a.valid_to is null or a.valid_to >= b.valid_from)
 order by a.valid_from, b.valid_from
"""

_OPEN_VERSIONS = """
select id, valid_from
  from provision_version
 where provision_id = %s
   and valid_to is null
 order by valid_from
"""

_SELECT_MATERIALITY_CACHE = "select response from llm_cache where cache_key = %s"

_INSERT_MATERIALITY_CACHE = """
insert into llm_cache (cache_key, prompt_version, model, request, response)
values (%s, %s, %s, %s, %s)
on conflict (cache_key) do nothing
"""

_UPSERT_MATERIALITY = """
insert into provision_materiality (
  from_version_id, to_version_id, prompt_version, material, confidence, evidence_span, model
)
values (%s, %s, %s, %s, %s, %s, %s)
on conflict (from_version_id, to_version_id, prompt_version) do update set
  material = excluded.material,
  confidence = excluded.confidence,
  evidence_span = excluded.evidence_span,
  model = excluded.model,
  created_at = now()
"""

# Every rewording a real decision is exposed to, with no verdict yet. Mirrors the shape of
# ProvisionRepository.RELIED_ON_SQL: the baseline is the citing decision's decided_on, the
# comparison is asOf, and only a pair whose *bodies* differ counts — two ids with the same
# text is a republication, not a rewording.
_PENDING_REWORDINGS = ("""
select distinct
       v0.id as from_version_id, v1.id as to_version_id,
       v0.body as from_body,     v1.body as to_body,
       v0.valid_from as from_valid_from, v1.valid_from as to_valid_from,
       p.act_no, p.section, p.subsec
  from citation c
  join decision d  on d.ecli = c.citing_ecli
  join provision p on p.id = c.cited_provision
  left join lateral (
{v0}
  ) v0 on true
  left join lateral (
{v1}
  ) v1 on true
 where c.cited_provision is not null
   and v0.id is not null
   and v1.id is not null
   and v0.id <> v1.id
   and v0.body <> v1.body
   and not exists (
       select 1
         from provision_materiality m
        where m.from_version_id = v0.id
          and m.to_version_id = v1.id
          and m.prompt_version = %s
   )
 order by v0.id, v1.id
""").format(
    v0=_VERSION_IN_FORCE.format(provision="p.id", date="d.decided_on"),
    v1=_VERSION_IN_FORCE.format(provision="p.id", date="%s"),
)


# ------------------------------------------------------------------------- ingest


@dataclass(frozen=True, slots=True)
class TimelineWrite:
    """What writing one provision's timeline changed."""

    provision_id: int
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0

    @property
    def touched(self) -> int:
        return self.inserted + self.updated + self.deleted


@dataclass(slots=True)
class IngestStats:
    """Totals for one ingest run."""

    records: int = 0
    provisions_seen: int = 0
    provisions_created: int = 0
    versions_inserted: int = 0
    versions_updated: int = 0
    versions_unchanged: int = 0
    versions_deleted: int = 0
    files: list[Path] = field(default_factory=list)

    def absorb(self, write: TimelineWrite, *, created: bool) -> None:
        self.provisions_seen += 1
        self.provisions_created += int(created)
        self.versions_inserted += write.inserted
        self.versions_updated += write.updated
        self.versions_unchanged += write.unchanged
        self.versions_deleted += write.deleted


def upsert_provision(conn: Conn, provision: ProvisionRecord) -> tuple[int, bool]:
    """``provision.id`` for one identity row, creating it if new. Returns ``(id, created)``."""
    key = provision.key()
    row = conn.execute(_FIND_PROVISION, key).fetchone()
    if row is not None:
        return int(row["id"]), False
    created = conn.execute(_INSERT_PROVISION, key).fetchone()
    if created is None:  # pragma: no cover - `returning id` always yields a row
        raise IngestError(f"insert into provision returned no id for {key}")
    return int(created["id"]), True


def _require_derogating_decisions(conn: Conn, versions: Sequence[ProvisionVersionRecord]) -> None:
    """Refuse a ``derogated_by`` that is not in ``decision``.

    ``provision_version.derogated_by`` is a foreign key onto ``decision(ecli)``, and the
    Java side dates a derogation by *that decision's* ``decided_on``. Quietly dropping an
    unknown ECLI would lose a red light; inventing the decision row would invent an
    identifier (CLAUDE.md rule 1). So it stops, and says which ECLI to crawl.
    """
    eclis = sorted({v.derogated_by for v in versions if v.derogated_by})
    if not eclis:
        return
    known = {
        row["ecli"]
        for row in conn.execute(
            "select ecli from decision where ecli = any(%s)", (eclis,)
        ).fetchall()
    }
    missing = [ecli for ecli in eclis if ecli not in known]
    if missing:
        raise IngestError(
            f"derogated_by names decision(s) not in the corpus: {', '.join(missing)}. "
            "provision_version.derogated_by is a foreign key onto decision(ecli), and the "
            "derogation is dated by that decision's decided_on, so the decision has to be "
            "crawled first. Not dropping it silently: that would lose a red light."
        )


def write_timeline(
    conn: Conn, provision_id: int, versions: Sequence[ProvisionVersionRecord]
) -> TimelineWrite:
    """Write one provision's closed timeline. Idempotent, and id-stable.

    Existing rows are matched to incoming versions **by ``valid_from``**, which is the
    natural key of a version within a provision. A matched row is updated in place when its
    body, end date or derogation changed, and left alone otherwise — so a re-run of the same
    dump performs zero writes and, more importantly, keeps every ``provision_version.id``
    that a ``provision_materiality`` row already points at.

    Rows the incoming timeline no longer contains are deleted, unless a materiality verdict
    references them, in which case it raises rather than either breaking the foreign key or
    leaving an overlapping window behind. Both of those are silent wrong-answer states; a
    refusal is not.

    ``versions`` is expected to be the output of
    :func:`~jg.crawl.esbirka.build_timeline`; it is re-run here so that a caller who
    assembles records by hand cannot write an overlapping timeline by accident.
    """
    closed = build_timeline(versions)
    _require_derogating_decisions(conn, closed)

    existing = {
        row["valid_from"]: row
        for row in conn.execute(_SELECT_VERSIONS, (provision_id,)).fetchall()
    }
    incoming = {record.valid_from: record for record in closed}

    inserted = updated = unchanged = 0
    for valid_from, record in incoming.items():
        row = existing.get(valid_from)
        if row is None:
            conn.execute(
                _INSERT_VERSION,
                (
                    provision_id,
                    record.body,
                    record.valid_from,
                    record.valid_to,
                    record.derogated_by,
                ),
            )
            inserted += 1
            continue
        same = (
            row["body"] == record.body
            and row["valid_to"] == record.valid_to
            and row["derogated_by"] == record.derogated_by
        )
        if same:
            unchanged += 1
            continue
        conn.execute(
            _UPDATE_VERSION, (record.body, record.valid_to, record.derogated_by, row["id"])
        )
        updated += 1

    obsolete = [row["id"] for valid_from, row in existing.items() if valid_from not in incoming]
    deleted = 0
    if obsolete:
        referenced = [
            int(row["id"])
            for row in conn.execute(_REFERENCED_VERSIONS, (obsolete,)).fetchall()
        ]
        if referenced:
            raise IngestError(
                f"provision {provision_id}: version(s) {referenced} are no longer in the "
                "dump but a provision_materiality verdict references them. Deleting would "
                "break the foreign key; keeping them would leave overlapping validity "
                "windows. Delete the materiality rows deliberately, then re-run."
            )
        for version_id in obsolete:
            conn.execute(_DELETE_VERSION, (version_id,))
            deleted += 1

    return TimelineWrite(
        provision_id=provision_id,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        deleted=deleted,
    )


def ingest_records(conn: Conn, records: Iterable[ProvisionVersionRecord]) -> IngestStats:
    """Load provision versions into ``provision`` / ``provision_version``.

    Groups by ``(act_no, section, subsec)``, closes each group's windows, writes each
    timeline. Running it twice over the same records is a no-op.
    """
    materialised = list(records)
    stats = IngestStats(records=len(materialised))
    for _key, group in group_by_provision(materialised).items():
        provision_id, created = upsert_provision(conn, group[0].provision)
        stats.absorb(write_timeline(conn, provision_id, group), created=created)
    return stats


def ingest_dumps(
    conn: Conn, directory: Path | None = None, *, limit: int | None = None
) -> IngestStats:
    """Ingest every :data:`~jg.crawl.esbirka.LOCAL_FORMAT` dump under ``directory``.

    Resumable from the on-disk cache by construction: the dump files are the cache, the
    write is idempotent, so an interrupted run is finished by running it again. ``limit``
    bounds the records read *per file*, which is how you take a first look at a dump you
    have not seen without reading all of it.
    """
    stats = IngestStats()
    for path in iter_dump_files(directory):
        raw = list(iter_dump_records(path, limit=limit))
        records = parse_local_dump(raw)
        log.info("%s: %d records", path, len(records))
        stats.files.append(path)
        file_stats = ingest_records(conn, records)
        stats.records += file_stats.records
        stats.provisions_seen += file_stats.provisions_seen
        stats.provisions_created += file_stats.provisions_created
        stats.versions_inserted += file_stats.versions_inserted
        stats.versions_updated += file_stats.versions_updated
        stats.versions_unchanged += file_stats.versions_unchanged
        stats.versions_deleted += file_stats.versions_deleted
    return stats


def ingest_act(
    conn: Conn,
    act_no: str,
    sections: Sequence[str],
    *,
    fetcher: Fetcher | None = None,
    dates: Iterable[dt.date] = (),
    all_wordings: bool = False,
) -> IngestStats:
    """Fetch some sections of one act from the live e-Sbírka API and write their timeline.

    This is milestone M6's actual entry point. ``dates`` are the dates a question is asked
    about — a decision's ``decided_on`` and the report's ``asOf`` — and each resolves to
    the wording in force on it; ``all_wordings`` loads the act's whole effective history
    instead, which is what removes the hole between two sampled dates.

    Everything downstream is the code that was already here and already tested:
    :func:`~jg.crawl.esbirka.build_timeline` closes the windows, :func:`write_timeline`
    writes them id-stably. A second run over the same act is a no-op — the fetch replays
    from ``data/raw/ESBIRKA/`` with zero network requests, and the write matches existing
    rows by ``valid_from``.

    The fetcher is injectable so a test can drive it against a cache directory of its own;
    it defaults to the ordinary allowlisted, rate-limited, cache-first one.
    """
    if not sections:
        raise IngestError(
            "no sections given. Fetching a whole act would be ~11 pages of statute per "
            "wording, and every row would need a provision identity nothing cites."
        )
    owned = fetcher is None
    active = fetcher if fetcher is not None else Fetcher(SOURCE_CODE)
    try:
        records = fetch_provision_versions(
            active, act_no, sections, dates=dates, all_wordings=all_wordings
        )
    finally:
        if owned:
            active.close()
    log.info(
        "e-Sbírka: %d version record(s) for act %s, section(s) %s",
        len(records),
        act_no,
        ", ".join(sections),
    )
    return ingest_records(conn, records)


# ------------------------------------------------------- reading the timeline back


def version_in_force(conn: Conn, provision_id: int, on_date: dt.date) -> dict[str, Any] | None:
    """The one version in force on ``on_date``, or ``None``.

    Character-for-character the question ``ProvisionRepository.VERSION_IN_FORCE`` asks. It
    is here so the ingest can be checked against the consumer rather than against its own
    assumptions: "which wording applied on date A, and on date B" is the entire provision
    layer, and if this returns two different rows for two dates then the amber in PLAN.md
    section 10 is real.
    """
    row = conn.execute(_VERSION_AT, (provision_id, on_date, on_date)).fetchone()
    return dict(row) if row is not None else None


def wording_change(
    conn: Conn, provision_id: int, decided_on: dt.date, as_of: dt.date
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """PLAN.md section 10 steps 2 and 3, as one call: ``(at_decided_on, at_as_of)``.

    Both halves come from :func:`version_in_force`, so this asks the stored timeline
    exactly what ``ProvisionRepository`` will ask it. Either side may be ``None`` when the
    corpus holds no wording for that date — a coverage gap, which is reported rather than
    papered over. The bodies being equal means no rewording; unequal is what
    :func:`judge_materiality` is then asked about.
    """
    return (
        version_in_force(conn, provision_id, decided_on),
        version_in_force(conn, provision_id, as_of),
    )


def timeline_problems(conn: Conn, provision_id: int) -> list[str]:
    """Invariant violations on one provision's stored timeline. Empty list means clean.

    Checks exactly what the lateral join depends on and cannot itself detect: no two
    windows overlap, and at most one version is left open. A stored timeline that fails
    either of these answers "what did this say on date X" with the wrong row rather than
    with no row, which is the failure mode that does not announce itself.
    """
    problems: list[str] = []
    for row in conn.execute(_OVERLAPS, (provision_id,)).fetchall():
        problems.append(
            f"version {row['earlier_id']} ({row['earlier_from']} .. "
            f"{row['earlier_to'] or 'open'}) overlaps version {row['later_id']} starting "
            f"{row['later_from']}"
        )
    open_versions = conn.execute(_OPEN_VERSIONS, (provision_id,)).fetchall()
    if len(open_versions) > 1:
        ids = ", ".join(str(row["id"]) for row in open_versions)
        problems.append(f"{len(open_versions)} versions have valid_to null ({ids}); expected 1")
    return problems


def pending_rewordings(
    conn: Conn, as_of: dt.date, *, prompt_version: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """Version pairs a real decision is exposed to, with no materiality verdict yet."""
    version = prompt_version or prompt_template(PROMPT_NAME).version
    # Three placeholders: the asOf lateral names the date twice (valid_from <= it, valid_to
    # >= it), then the prompt version in the `not exists`.
    rows = conn.execute(_PENDING_REWORDINGS, (as_of, as_of, version)).fetchall()
    return [dict(row) for row in (rows[:limit] if limit is not None else rows)]


# -------------------------------------------------------------------- materiality


@dataclass(frozen=True, slots=True)
class MaterialityVerdict:
    """A validated materiality judgement. Only ever constructed after the span gate."""

    from_version_id: int
    to_version_id: int
    material: bool
    confidence: float
    evidence_span: str
    model: str
    prompt_version: str


def build_materiality_prompt(
    provision: ProvisionRecord,
    from_body: str,
    to_body: str,
    from_valid_from: dt.date,
    to_valid_from: dt.date,
    template: PromptTemplate | None = None,
) -> str:
    """Render the frozen ``provision-materiality.v1.md`` for one version pair."""
    active = template or prompt_template(PROMPT_NAME)
    return active.render(
        act_no=provision.act_no,
        section=provision.section,
        # The template writes "§ {{section}} {{subsec}} zákona č. {{act_no}} Sb.", so the
        # subsection has to carry its own "odst." here. Empty string, never None: a
        # provision cited without a subsection is a normal state, and PromptTemplate.render
        # rejects None precisely so the caller has to decide what the model is told.
        subsec=f"odst. {provision.subsec}" if provision.subsec else "",
        from_valid_from=from_valid_from.isoformat(),
        to_valid_from=to_valid_from.isoformat(),
        from_body=from_body,
        to_body=to_body,
    )


def retry_prompt(prompt: str, violation: str) -> str:
    """The same prompt with the violation stated. The frozen template is not touched."""
    return (
        f"{prompt}\n\n---\n\n"
        f"Your previous answer was rejected: {violation}\n\n"
        "Return one JSON object in the same shape. `evidence_span` must be copied character "
        "for character out of one of the two wordings above. Quote the clause that changed."
    )


def parse_materiality(
    payload: Mapping[str, Any], from_body: str, to_body: str
) -> tuple[bool, float, str]:
    """Validate one materiality response. Returns ``(material, confidence, span)`` or raises.

    CLAUDE.md rule 3, applied to this prompt: the span must occur verbatim, after whitespace
    normalisation, in **one of the two wordings** — which is what the frozen template asks
    for and what it promises is checked mechanically. Case, diacritics and punctuation must
    match; in Czech legal prose they carry the meaning the verdict stands on.

    Raises :class:`~jg.classify.reasoning.ResponseRejected`, whose message is written back
    into the retry, so it has to read as an instruction rather than as a log line.
    """
    material = payload.get("material")
    if not isinstance(material, bool):
        raise ResponseRejected(f"`material` was {material!r}, which is not true or false.")

    confidence_raw = payload.get("confidence")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        raise ResponseRejected(f"`confidence` was {confidence_raw!r}, which is not a number.")
    confidence = float(confidence_raw)
    if not 0.0 <= confidence <= 1.0:
        raise ResponseRejected(f"`confidence` was {confidence}, which is outside [0, 1].")

    span = payload.get("evidence_span")
    if not isinstance(span, str) or not span.strip():
        raise ResponseRejected("`evidence_span` was empty.")
    if not (is_verbatim(span, from_body) or is_verbatim(span, to_body)):
        shortened = normalise_ws(span)
        if len(shortened) > 120:
            shortened = f"{shortened[:120]}..."
        raise ResponseRejected(
            f"`evidence_span` was {shortened!r}, which occurs in neither wording. It must be "
            "copied character for character out of one of the two blocks above."
        )
    return material, round(confidence, 2), normalise_ws(span)


def cache_key(from_version_id: int, to_version_id: int, prompt_version: str) -> str:
    """``llm_cache.cache_key`` for a materiality judgement.

    The documented key is ``(from_version_id, to_version_id, prompt_version)`` — the version
    pair, not the decision that triggered it, exactly as PLAN.md section 10 requires, which
    is what makes the hit rate high: many decisions rely on the same provision.
    """
    return "|".join(("materiality", str(from_version_id), str(to_version_id), prompt_version))


def judge_materiality(
    conn: Conn,
    pair: Mapping[str, Any],
    *,
    model: MaterialityCall | None = None,
    model_name: str | None = None,
    template: PromptTemplate | None = None,
    store: bool = True,
) -> MaterialityVerdict | None:
    """Judge one rewording. Returns ``None`` when nothing storable was produced.

    ``pair`` is one row of :func:`pending_rewordings`. ``model`` defaults to ``None`` and is
    then built from the environment, which raises
    :class:`~jg.classify.reasoning.ModelUnavailable` — a named error the CLI turns into a
    sentence — rather than crashing on a missing key.

    Sequence: cache lookup (re-validated on the way out, so a poisoned cache cannot smuggle
    a verdict past the gate), one call, validate, one retry with the violation stated,
    validate again. Two failures and **nothing is written**. ``provision_materiality`` has no
    ``UNCLASSIFIED`` state and inventing one would be storing an unvalidated label.
    """
    active = template or prompt_template(PROMPT_NAME)
    # The configured provider's model ID, read from the environment without building a
    # client: a cache hit must still resolve a name to record, and must still cost neither
    # an API key nor a request.
    name = model_name or llm_model_name()
    provision = ProvisionRecord(
        act_no=pair["act_no"], section=pair["section"], subsec=pair["subsec"]
    )
    from_body, to_body = pair["from_body"], pair["to_body"]
    from_id, to_id = int(pair["from_version_id"]), int(pair["to_version_id"])
    prompt = build_materiality_prompt(
        provision,
        from_body,
        to_body,
        pair["from_valid_from"],
        pair["to_valid_from"],
        active,
    )
    key = cache_key(from_id, to_id, active.version)

    cached = conn.execute(_SELECT_MATERIALITY_CACHE, (key,)).fetchone()
    if cached is not None and isinstance(cached["response"], dict):
        try:
            material, confidence, span = parse_materiality(
                cached["response"], from_body, to_body
            )
        except ResponseRejected as exc:
            log.warning("cached materiality for %s rejected (%s); re-asking", key, exc.violation)
        else:
            verdict = MaterialityVerdict(
                from_id, to_id, material, confidence, span, name, active.version
            )
            if store:
                store_materiality(conn, verdict)
            return verdict

    # Built only now, after the cache lookup: a resumed run whose pairs are all cached needs
    # no key at all. The provider is chosen from the environment; Anthropic's factory is
    # handed over rather than imported, so `jg.gemini` keeps no edge back to this module.
    call = (
        model
        if model is not None
        else provider_model(
            RESPONSE_SCHEMA, anthropic=anthropic_materiality_model, model=name
        )
    )

    attempt = prompt
    violation: str | None = None
    for _ in range(2):
        payload = dict(call(attempt))
        try:
            material, confidence, span = parse_materiality(payload, from_body, to_body)
        except ResponseRejected as exc:
            violation = exc.violation
            log.warning(
                "%s (versions %d -> %d): response rejected: %s",
                provision.label(),
                from_id,
                to_id,
                exc.violation,
            )
            attempt = retry_prompt(prompt, exc.violation)
            continue
        conn.execute(
            _INSERT_MATERIALITY_CACHE,
            (
                key,
                active.version,
                name,
                Json({"prompt": attempt, "prompt_version": active.version, "model": name}),
                Json(payload),
            ),
        )
        verdict = MaterialityVerdict(
            from_id, to_id, material, confidence, span, name, active.version
        )
        if store:
            store_materiality(conn, verdict)
        return verdict

    log.error(
        "%s (versions %d -> %d): no materiality verdict stored after two failed "
        "validations (%s). The rewording stays unjudged, which reads as not material.",
        provision.label(),
        from_id,
        to_id,
        violation,
    )
    return None


def store_materiality(conn: Conn, verdict: MaterialityVerdict) -> None:
    """Write one validated verdict into ``provision_materiality``."""
    conn.execute(
        _UPSERT_MATERIALITY,
        (
            verdict.from_version_id,
            verdict.to_version_id,
            verdict.prompt_version,
            verdict.material,
            verdict.confidence,
            verdict.evidence_span,
            verdict.model,
        ),
    )


def run_materiality(
    conn: Conn,
    as_of: dt.date,
    *,
    model: MaterialityCall | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Judge every pending rewording. Returns ``(judged, skipped)``."""
    pending = pending_rewordings(conn, as_of, limit=limit)
    judged = skipped = 0
    for pair in pending:
        if judge_materiality(conn, pair, model=model) is None:
            skipped += 1
        else:
            judged += 1
    return judged, skipped


def anthropic_materiality_model(
    *,
    model: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> MaterialityCall:
    """A :class:`MaterialityCall` over the Anthropic Messages API. ``httpx``, per section 4.

    Constrained to :data:`RESPONSE_SCHEMA` so the parse cannot drift from the prompt's
    Output section. On ``temperature``: the same caveat as
    ``jg.classify.reasoning.anthropic_model`` — current Claude models reject the sampling
    parameters, so none is sent by default, which satisfies what CLAUDE.md rule 7 protects
    (no variance between two runs) while deviating from its letter. Flagged, not hidden.
    """
    key = api_key or llm_api_key()
    if not key:
        raise ModelUnavailable(
            "ANTHROPIC_API_KEY is not set, so no materiality judgement can be made. "
            "Ingest and the timeline queries need no key; only this call does."
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
            f"no text block in the response (stop_reason={body.get('stop_reason')!r})."
        )

    return call


# --------------------------------------------------------------------------- CLI

# `jg provisions ...` would need a command added to jg/cli.py, which another agent holds
# right now, so this stage is reachable as `python -m jg.provisions ...` instead. Wiring it
# into the main CLI is a lazy-import command body of the same four lines as `jg extract`.


def _app():  # pragma: no cover - thin CLI wiring
    import typer
    from rich.console import Console
    from rich.table import Table

    console = Console()
    err = Console(stderr=True)
    app = typer.Typer(
        name="jg-provisions",
        help="Ustanovení: načtení verzí do databáze a posouzení podstatnosti změny.",
        no_args_is_help=True,
        add_completion=False,
    )

    @app.command()
    def ingest(
        directory: str | None = typer.Option(None, "--dir", help="Adresář s dumpy"),
        limit: int | None = typer.Option(None, "--limit", help="Max. záznamů na soubor"),
    ) -> None:
        """Načte dumpy do tabulek provision a provision_version. Opakované spuštění nic nemění."""
        from jg.db import connect

        with connect() as conn:
            stats = ingest_dumps(conn, Path(directory) if directory else None, limit=limit)
        if not stats.files:
            err.print(
                f"[yellow]Žádné dumpy nenalezeny.[/yellow] Formát: {LOCAL_FORMAT}. "
                "e-Sbírka není dostupná; viz jg.crawl.esbirka."
            )
            raise typer.Exit(1)
        table = Table(title="Načtení ustanovení", show_edge=False)
        table.add_column("Ukazatel")
        table.add_column("Hodnota", justify="right")
        table.add_row("Soubory", str(len(stats.files)))
        table.add_row("Záznamy", str(stats.records))
        table.add_row("Ustanovení", str(stats.provisions_seen))
        table.add_row("  nově založená", str(stats.provisions_created))
        table.add_row("Verze vložené", str(stats.versions_inserted))
        table.add_row("Verze změněné", str(stats.versions_updated))
        table.add_row("Verze beze změny", str(stats.versions_unchanged))
        table.add_row("Verze smazané", str(stats.versions_deleted))
        console.print(table)

    # Bound to names first rather than written inline as defaults: `from __future__ import
    # annotations` turns every annotation into a string, and typer evaluates those against
    # the *module* globals, where `typer` — imported inside this factory — does not exist.
    # So `Annotated[..., typer.Option(...)]` cannot be used here, and a bare call in a
    # default position trips ruff's B008.
    act_opt = typer.Option(..., "--act", help="Číslo předpisu, např. 89/2012")
    section_opt = typer.Option(..., "--section", help="Číslo §, lze opakovat")
    date_opt = typer.Option(None, "--date", help="Datum, k němuž se ptáme (lze opakovat)")
    all_wordings_opt = typer.Option(
        False, "--all-wordings", help="Načte celou účinnou historii předpisu"
    )

    @app.command()
    def fetch(
        act: str = act_opt,
        section: list[str] = section_opt,
        date: list[str] | None = date_opt,
        all_wordings: bool = all_wordings_opt,
    ) -> None:
        """Načte znění vybraných § z e-Sbírky. Opakované spuštění nesahá na síť."""
        from jg.db import connect

        dates = [dt.date.fromisoformat(value) for value in (date or [])]
        if not dates and not all_wordings:
            err.print("[yellow]Zadejte alespoň jedno --date, nebo --all-wordings.[/yellow]")
            raise typer.Exit(2)
        with connect() as conn:
            stats = ingest_act(
                conn, act, section, dates=dates, all_wordings=all_wordings
            )
        table = Table(title=f"e-Sbírka: {act}", show_edge=False)
        table.add_column("Ukazatel")
        table.add_column("Hodnota", justify="right")
        table.add_row("Záznamy znění", str(stats.records))
        table.add_row("Ustanovení", str(stats.provisions_seen))
        table.add_row("  nově založená", str(stats.provisions_created))
        table.add_row("Verze vložené", str(stats.versions_inserted))
        table.add_row("Verze změněné", str(stats.versions_updated))
        table.add_row("Verze beze změny", str(stats.versions_unchanged))
        table.add_row("Verze smazané", str(stats.versions_deleted))
        console.print(table)

    @app.command()
    def compare(
        provision_id: int,
        decided_on: str = typer.Option(..., "--decided-on", help="Datum rozhodnutí"),
        as_of: str = typer.Option(..., "--as-of", help="Datum, ke kterému se posuzuje"),
    ) -> None:
        """Porovná znění účinné v den rozhodnutí se zněním účinným k danému dni."""
        from jg.db import connect

        with connect() as conn:
            before, after = wording_change(
                conn,
                provision_id,
                dt.date.fromisoformat(decided_on),
                dt.date.fromisoformat(as_of),
            )
        if before is None or after is None:
            err.print(
                "[yellow]Pro jedno z dat není uloženo žádné znění.[/yellow] "
                "Načtěte je příkazem fetch."
            )
            raise typer.Exit(1)
        if before["body"] == after["body"]:
            console.print("[green]Znění se nezměnilo.[/green]")
            return
        console.print(f"[yellow]Znění bylo změněno.[/yellow] Ke dni {decided_on} "
                      f"(verze {before['id']}, od {before['valid_from']}):\n{before['body']}\n")
        console.print(f"Ke dni {as_of} (verze {after['id']}, od {after['valid_from']}):\n"
                      f"{after['body']}")

    @app.command("in-force")
    def in_force(provision_id: int, date: str) -> None:
        """Které znění platilo k danému dni."""
        from jg.db import connect

        with connect() as conn:
            row = version_in_force(conn, provision_id, dt.date.fromisoformat(date))
        if row is None:
            console.print(f"[yellow]K {date} neplatilo žádné znění.[/yellow]")
            return
        console.print(f"[green]verze {row['id']}[/green] od {row['valid_from']} "
                      f"do {row['valid_to'] or '—'}\n{row['body']}")

    @app.command()
    def check(provision_id: int) -> None:
        """Ověří, že okna platnosti nekolidují a otevřené je nejvýše jedno."""
        from jg.db import connect

        with connect() as conn:
            problems = timeline_problems(conn, provision_id)
        if not problems:
            console.print("[green]Časová osa je v pořádku.[/green]")
            return
        for problem in problems:
            err.print(f"[red]{problem}[/red]")
        raise typer.Exit(1)

    @app.command()
    def materiality(
        as_of: str = typer.Option(..., "--as-of", help="Datum, ke kterému se posuzuje"),
        limit: int | None = typer.Option(None, "--limit"),
    ) -> None:
        """Posoudí u každé změny znění, zda mohla ovlivnit právní úvahu."""
        from jg.db import connect

        try:
            with connect() as conn:
                judged, skipped = run_materiality(
                    conn, dt.date.fromisoformat(as_of), limit=limit
                )
        except ModelUnavailable as exc:
            err.print(f"[yellow]Model není dostupný:[/yellow] {exc}")
            raise typer.Exit(1) from exc
        console.print(f"Posouzeno: {judged}, neuloženo po dvou neplatných odpovědích: {skipped}")

    return app


def main() -> None:  # pragma: no cover - entry point
    _app()()


if __name__ == "__main__":  # pragma: no cover
    main()

"""The classification router: three tiers, cheapest first. PLAN.md section 8, M3 and M5.

Cost control lives in this module. Every citation edge in the corpus passes through
:func:`route`, which either finishes it without a model call or says why it needs one:

1. **structural** (:mod:`jg.classify.structural`) — party-submission ``MENTIONED``,
   výrok-based ``QUASHED``. No model, no network.
2. **forced escalation** — the citing decision is a *rozšířený senát* / *velký senát* /
   *plénum*, or the surrounding text carries a departure marker. These are the cases where
   the interpretation actually changes, so they are never triaged cheaply.
3. **triage, then escalate** — everything else goes to a cheap classifier; anything below
   confidence 0.75 escalates.

:func:`route` never calls a model. It returns either a finished ``TreatmentResult`` or an
:class:`Escalation`, which is what makes :func:`dry_run` possible: the exact tier
distribution of a corpus, measured, with no API key and nothing spent. PLAN.md section 8
projects roughly 20k edges reducing to about 1500 reasoning calls on a 3000-decision
corpus; that projection is a claim about this router, and the dry run is how it is checked
rather than asserted.
"""

from __future__ import annotations

import logging
import os
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple

from rich.console import Console
from rich.table import Table

from jg.classify.prompts import load_prompt
from jg.classify.reasoning import (
    PROMPT_NAME,
    DbResponseCache,
    ModelUnavailable,
    anthropic_model,
    classify_edge,
    is_verbatim,
)
from jg.classify.structural import (
    QUASHING_ALIAS_KINDS,
    RULESET_VERSION,
    CitationEdge,
    MarkerSet,
    as_panel_type,
    citation_sentence,
    classify_structural,
    departure_span,
    marker_set,
)
from jg.config import llm_api_key
from jg.db import Conn, connect
from jg.models import PanelType, Route, TreatmentLabel, TreatmentResult

log = logging.getLogger(__name__)

#: Panels that may depart from their own court's case law (``departure_authority``, V2), so
#: the ones whose citations are worth a reasoning call whatever the language looks like.
#: A referral *to* one of these is an ordinary panel and is not in this set — see
#: ``tests/test_panel_type.py``, which is why the departure markers include *postoupil*.
DEPARTURE_PANELS = frozenset({PanelType.EXTENDED, PanelType.GRAND, PanelType.PLENARY})

#: PLAN.md section 8: "Escalate anything below confidence 0.75."
TRIAGE_THRESHOLD = 0.75

#: What :func:`default_triage` claims for an edge with no adverse signal anywhere in it.
#: Above the threshold, and deliberately not higher: it is a cheap-tier guess.
TRIAGE_CONFIDENCE = 0.80

#: ``treatment.prompt_version`` for a triage row. Names the ruleset, as in
#: :data:`jg.classify.structural.RULESET_VERSION`; no prompt file is involved.
TRIAGE_RULESET_VERSION = "triage-lexical.v1"

#: Paragraphs either side of the citing paragraph in the context window. PLAN.md section 8.
CONTEXT_RADIUS = 2

#: Set to 1 to make ``jg classify`` route and report without writing rows or calling a
#: model. A stopgap: the statistics mode belongs behind a ``--dry-run`` flag on
#: ``jg.cli.classify``, which this task may not edit.
DRY_RUN_ENV = "JG_CLASSIFY_DRY_RUN"


class EscalationReason(StrEnum):
    """Why an edge could not be finished cheaply."""

    #: Citing decision is an extended / grand panel or the plenum.
    PANEL_TYPE = "panel_type"
    #: A departure marker occurs in the context window.
    DEPARTURE_MARKER = "departure_marker"
    #: Triage was not confident enough, or could not quote its own evidence.
    LOW_CONFIDENCE = "low_confidence"


class TriageVerdict(NamedTuple):
    """What the cheap tier thinks. ``evidence_span`` must be a quote from the context."""

    label: TreatmentLabel
    confidence: float
    evidence_span: str


class Escalation(NamedTuple):
    """:func:`route` declining to finish an edge, with the reason it declined."""

    edge: CitationEdge
    reason: EscalationReason
    #: What triage said before we overruled it, when it said anything. For the report.
    triaged: TriageVerdict | None = None


#: A routing decision: a finished label, or a reason to spend a model call.
Routing = TreatmentResult | Escalation

#: The cheap classifier. Pure by default; a small model can be injected in its place.
Triage = Callable[[CitationEdge], TriageVerdict]


def default_triage(edge: CitationEdge) -> TriageVerdict:
    """The cheap tier, with no model behind it and no vocabulary of its own.

    By the time an edge reaches tier 3 the corpus has already told us a good deal: no
    quashing in the výrok, no party-submission passage, an ordinary panel, and no departure
    marker anywhere in the context window. What is left is the modal Czech citation — a
    supporting reference in the court's own reasoning — and the strongest claim the evidence
    supports is that nothing adverse is being done to the cited decision.

    It is labelled ``FOLLOWED`` rather than ``MENTIONED`` because the citation is in the
    court's own text, and the choice between those two is cheap to get wrong on purpose:
    the rules engine (PLAN.md section 9) maps both to GREEN, so a mislabel here cannot move
    a traffic light. It *can* cost per-label recall in the eval (section 15), which is the
    honest price of not spending a call on every edge in the corpus.

    The one thing it refuses to do is claim a label it cannot quote for: an edge whose
    citation is not locatable in its own paragraph gets confidence 0.0 and escalates.
    """
    span = citation_sentence(edge.citing_paragraph, edge.raw_text)
    if span is None:
        return TriageVerdict(TreatmentLabel.MENTIONED, 0.0, "")
    return TriageVerdict(TreatmentLabel.FOLLOWED, TRIAGE_CONFIDENCE, span)


def route(
    edge: CitationEdge,
    *,
    triage: Triage = default_triage,
    markers: MarkerSet | None = None,
) -> Routing:
    """Route one edge. Never calls a model; never touches the database.

    Tier order is PLAN.md's, and the first two rules deliberately outrank the forced
    escalation below them: a citation inside a party's submission is a report of what a
    party argued whatever panel is doing the reporting, and an annulment named in the výrok
    is already certain. Sending either to a reasoning model buys nothing.
    """
    active = markers or marker_set()

    structural = classify_structural(edge, active)
    if structural is not None:
        return structural

    if edge.citing_panel in DEPARTURE_PANELS:
        return Escalation(edge, EscalationReason.PANEL_TYPE)

    if departure_span(edge.context_text, active) is not None:
        return Escalation(edge, EscalationReason.DEPARTURE_MARKER)

    verdict = triage(edge)
    if verdict.confidence < TRIAGE_THRESHOLD:
        return Escalation(edge, EscalationReason.LOW_CONFIDENCE, verdict)
    if not is_verbatim(verdict.evidence_span, edge.context_text):
        # The span gate is not only for the reasoning tier. An injected triage model is a
        # model, and CLAUDE.md rule 3 does not care which tier produced the label.
        log.warning(
            "citation %s: triage span is not a quote from the context; escalating",
            edge.citation_id,
        )
        return Escalation(edge, EscalationReason.LOW_CONFIDENCE, verdict)

    return TreatmentResult(
        citation_id=edge.citation_id,
        label=verdict.label,
        confidence=round(verdict.confidence, 2),
        evidence_span=verdict.evidence_span,
        route=Route.TRIAGE,
        model=None,
        prompt_version=TRIAGE_RULESET_VERSION,
    )


# ---------------------------------------------------------------------- statistics


@dataclass
class RouterStats:
    """Where a corpus of edges landed. The output of the dry run and of a real run."""

    total: int = 0
    #: label -> count, for rows finished without a model call.
    structural: Counter[str] = field(default_factory=Counter)
    triage: Counter[str] = field(default_factory=Counter)
    #: escalation reason -> count.
    escalated: Counter[str] = field(default_factory=Counter)
    #: label -> count, for rows the reasoning model actually produced. Empty after a dry run.
    reasoning: Counter[str] = field(default_factory=Counter)

    def record(self, routing: Routing) -> None:
        self.total += 1
        if isinstance(routing, Escalation):
            self.escalated[str(routing.reason)] += 1
        elif routing.route is Route.STRUCTURAL:
            self.structural[str(routing.label)] += 1
        else:
            self.triage[str(routing.label)] += 1

    def record_reasoning(self, result: TreatmentResult) -> None:
        self.reasoning[str(result.label)] += 1

    @property
    def reasoning_calls(self) -> int:
        """Edges that need a model call. The number the cost projection is about."""
        return sum(self.escalated.values())

    @property
    def reasoning_share(self) -> float:
        return 0.0 if self.total == 0 else self.reasoning_calls / self.total

    @property
    def unclassified_share(self) -> float:
        """``UNCLASSIFIED`` as a fraction of reasoning calls. M5 wants this under 5%."""
        produced = sum(self.reasoning.values())
        if produced == 0:
            return 0.0
        return self.reasoning[str(TreatmentLabel.UNCLASSIFIED)] / produced


def route_all(
    edges: Iterable[CitationEdge],
    *,
    triage: Triage = default_triage,
    markers: MarkerSet | None = None,
) -> tuple[list[Routing], RouterStats]:
    """Route a whole corpus. Returns every decision plus the tier distribution."""
    active = markers or marker_set()
    stats = RouterStats()
    routings: list[Routing] = []
    for edge in edges:
        routing = route(edge, triage=triage, markers=active)
        stats.record(routing)
        routings.append(routing)
    return routings, stats


def dry_run(
    edges: Iterable[CitationEdge],
    *,
    triage: Triage = default_triage,
    markers: MarkerSet | None = None,
) -> RouterStats:
    """Tier distribution only. No model call, no write, nothing spent."""
    _routings, stats = route_all(edges, triage=triage, markers=markers)
    log.info(
        "router: %d edges -> %d reasoning calls (%.1f%%)",
        stats.total,
        stats.reasoning_calls,
        100 * stats.reasoning_share,
    )
    return stats


#: Czech gloss for the escalation reasons. The reason codes themselves stay English, like
#: every other internal identifier (CLAUDE.md, "Language and terminology").
_REASON_CS = {
    "panel_type": "panel_type: rozšířený/velký senát nebo plénum",
    "departure_marker": "departure_marker: marker odchýlení v kontextu",
    "low_confidence": "low_confidence: triage si není jistá",
}


def stats_table(stats: RouterStats, *, title: str = "Směrování klasifikace") -> Table:
    """The dry-run / run report, in Czech, in the shape ``jg.cli`` prints elsewhere."""
    table = Table(title=title, show_edge=False)
    table.add_column("Vrstva")
    table.add_column("Hran", justify="right")
    table.add_column("Podíl", justify="right")

    def share(count: int) -> str:
        return "—" if stats.total == 0 else f"{count / stats.total:.1%}"

    structural_total = sum(stats.structural.values())
    triage_total = sum(stats.triage.values())
    table.add_row("strukturální (bez modelu)", str(structural_total), share(structural_total))
    for label, count in sorted(stats.structural.items()):
        table.add_row(f"  {label}", str(count), "")
    table.add_row("triage (bez modelu)", str(triage_total), share(triage_total))
    for label, count in sorted(stats.triage.items()):
        table.add_row(f"  {label}", str(count), "")
    table.add_section()
    table.add_row("eskalace do modelu", str(stats.reasoning_calls), share(stats.reasoning_calls))
    for reason, count in sorted(stats.escalated.items()):
        table.add_row(f"  {_REASON_CS.get(reason, reason)}", str(count), "")
    if stats.reasoning:
        table.add_section()
        table.add_row("modelem označeno", str(sum(stats.reasoning.values())), "")
        for label, count in sorted(stats.reasoning.items()):
            table.add_row(f"  {label}", str(count), "")
        table.add_row("  podíl UNCLASSIFIED", f"{stats.unclassified_share:.1%}", "")
    table.add_section()
    table.add_row("hran celkem", str(stats.total), "")
    return table


# --------------------------------------------------------------------- loading edges


_SELECT_EDGES = """
select c.id                as citation_id,
       c.citing_ecli       as citing_ecli,
       c.cited_ecli        as cited_ecli,
       c.paragraph_idx     as paragraph_idx,
       c.raw_text          as raw_text,
       citing.court_code   as citing_court,
       citing.panel_type   as citing_panel,
       citing.decided_on   as citing_date,
       citing.source_url   as citing_source_url,
       cited.court_code    as cited_court,
       cited.decided_on    as cited_date,
       cited.ratio_summary as cited_ratio
  from citation c
  join decision citing on citing.ecli = c.citing_ecli
  join decision cited  on cited.ecli  = c.cited_ecli
 where c.cited_ecli is not null
   and (%s::text is null or citing.court_code = %s)
   and not exists (
     select 1
       from treatment t
      where t.citation_id = c.id
        and t.prompt_version = any(%s)
   )
 order by c.citing_ecli, c.paragraph_idx, c.id
"""

_SELECT_PARAGRAPHS = """
select idx, body
  from decision_paragraph
 where ecli = %s
 order by idx
"""

_SELECT_ALIASES = """
select ecli, alias
  from decision_alias
 where ecli = any(%s)
   and alias_kind = any(%s)
"""

# `treatment` is unique on (citation_id, prompt_version), so a re-run is a no-op rather than
# a duplicate. A *new* prompt version is a new row, which is what lets two versions be
# compared in the eval without deleting anything.
_INSERT_TREATMENT = """
insert into treatment (
    citation_id, label, confidence, evidence_span, route, model, prompt_version
) values (%s, %s, %s, %s, %s, %s, %s)
on conflict (citation_id, prompt_version) do nothing
"""


def context_window(
    paragraphs: Sequence[tuple[int, str]],
    paragraph_idx: int | None,
    radius: int = CONTEXT_RADIUS,
) -> tuple[tuple[str, ...], int]:
    """``(window, focus)``: the citing paragraph plus ``radius`` either side. Pure.

    ``paragraphs`` is ``(idx, body)`` ordered by ``idx``. Windowing walks that list rather
    than doing arithmetic on ``idx`` because ``decision_paragraph.idx`` legitimately has
    gaps (``jg.normalize.paragraph_rows`` drops blank paragraphs without renumbering, so
    stored ``citation.paragraph_idx`` values stay valid). "Two paragraphs either side" then
    means two *real* paragraphs, not two index steps.

    An unknown or missing paragraph returns an empty window: there is no context to quote
    from, and an edge with no context escalates and is honestly reported rather than being
    classified against a paragraph we guessed at.
    """
    if paragraph_idx is None or not paragraphs:
        return (), 0
    indices = [idx for idx, _body in paragraphs]
    position = bisect_left(indices, paragraph_idx)
    if position >= len(indices) or indices[position] != paragraph_idx:
        log.debug("paragraph %s absent from the stored window", paragraph_idx)
        return (), 0
    start = max(position - radius, 0)
    end = min(position + radius + 1, len(paragraphs))
    return tuple(body for _idx, body in paragraphs[start:end]), position - start


#: ``citing_ecli -> výrok``. Supplied by the caller; see :func:`crawl_cache_verdicts`.
VerdictLookup = Callable[[str], str | None]


def crawl_cache_verdicts(rows: Sequence[tuple[str, str, str]]) -> dict[str, str]:
    """``{ecli: výrok}`` read out of ``data/raw/``, for ``(ecli, court_code, source_url)``.

    ``decision`` has no column for the operative part (``jg.normalize.store_decision`` says
    so explicitly and points here), so the only place the výrok exists after a crawl is the
    cached page it was parsed from. This reads those pages off disk and re-parses them with
    the court module's own pure parser: **zero network requests**, no re-crawl, and no
    second copy of the parsing logic.

    Courts whose module has no text parser yet, and decisions whose page is not in the
    cache, simply yield nothing. The consequence is stated in the run report rather than
    hidden: with no výrok there is no structural ``QUASHED`` for that decision.
    """
    verdicts: dict[str, str] = {}
    try:
        from jg.crawl import SOURCES
        from jg.crawl.base import Fetcher
    except ImportError:  # pragma: no cover - only when the crawl stage is absent
        log.warning("jg.crawl unavailable: no výrok available, so no structural QUASHED")
        return verdicts

    by_court: dict[str, list[tuple[str, str]]] = {}
    for ecli, court_code, source_url in rows:
        by_court.setdefault(court_code, []).append((ecli, source_url))

    for court_code, decisions in by_court.items():
        module = SOURCES.get(court_code)
        parse = getattr(module, "parse_gettext", None)
        if parse is None:
            log.info("%s: crawl module exposes no decision-text parser; no výrok", court_code)
            continue
        with Fetcher(court_code) as fetcher:
            for ecli, source_url in decisions:
                body_path, _sidecar = fetcher.cache_paths(source_url)
                if not body_path.exists():
                    continue
                try:
                    document = parse(body_path.read_text(encoding="utf-8"))
                except Exception:  # a stale cached page must not stop a batch
                    log.warning("could not re-parse cached page for %s", ecli, exc_info=True)
                    continue
                verdict = getattr(document, "verdict_text", None)
                if verdict:
                    verdicts[ecli] = verdict
    return verdicts


def written_versions() -> list[str]:
    """The ``prompt_version`` values a run may write. Used to skip already-labelled edges."""
    versions = [RULESET_VERSION, TRIAGE_RULESET_VERSION]
    try:
        versions.append(load_prompt(PROMPT_NAME).version)
    except Exception:  # a missing template must not block the cheap tiers
        log.warning("could not read the version of %s", PROMPT_NAME, exc_info=True)
    return versions


def load_edges(
    conn: Conn,
    court: str | None = None,
    *,
    verdicts: VerdictLookup | None = None,
) -> list[CitationEdge]:
    """Every unlabelled decision-to-decision edge, with its context window attached.

    Provision citations are out of scope here: a ``treatment`` label describes what one
    decision does to another, and a provision is handled by the rewording check (PLAN.md
    section 10). They are left for that stage rather than labelled.
    """
    rows = conn.execute(_SELECT_EDGES, (court, court, written_versions())).fetchall()
    if not rows:
        return []

    cited_eclis = sorted({row["cited_ecli"] for row in rows})
    aliases: dict[str, list[str]] = {}
    for alias_row in conn.execute(
        _SELECT_ALIASES, (cited_eclis, list(QUASHING_ALIAS_KINDS))
    ).fetchall():
        aliases.setdefault(alias_row["ecli"], []).append(alias_row["alias"])

    citing = {(row["citing_ecli"], row["citing_court"], row["citing_source_url"]) for row in rows}
    lookup = verdicts or crawl_cache_verdicts(sorted(citing)).get

    paragraphs: dict[str, list[tuple[int, str]]] = {}
    edges: list[CitationEdge] = []
    for row in rows:
        citing_ecli = row["citing_ecli"]
        if citing_ecli not in paragraphs:
            paragraphs[citing_ecli] = [
                (int(p["idx"]), p["body"])
                for p in conn.execute(_SELECT_PARAGRAPHS, (citing_ecli,)).fetchall()
            ]
        window, focus = context_window(paragraphs[citing_ecli], row["paragraph_idx"])
        edges.append(
            CitationEdge(
                citation_id=int(row["citation_id"]),
                citing_ecli=citing_ecli,
                citing_court=row["citing_court"],
                citing_panel=as_panel_type(row["citing_panel"]),
                citing_date=row["citing_date"],
                cited_ecli=row["cited_ecli"],
                cited_court=row["cited_court"],
                cited_date=row["cited_date"],
                cited_ratio=row["cited_ratio"],
                cited_aliases=tuple(aliases.get(row["cited_ecli"], ())),
                paragraph_idx=row["paragraph_idx"],
                raw_text=row["raw_text"],
                context=window,
                context_focus=focus,
                verdict_text=lookup(citing_ecli),
            )
        )
    return edges


def write_treatment(conn: Conn, result: TreatmentResult) -> int:
    """Persist one label. Returns the number of rows written (0 when already present)."""
    cursor = conn.execute(
        _INSERT_TREATMENT,
        (
            result.citation_id,
            str(result.label),
            result.confidence,
            result.evidence_span,
            str(result.route),
            result.model,
            result.prompt_version,
        ),
    )
    return cursor.rowcount


# ------------------------------------------------------------------------- the run


def _dry_run_requested() -> bool:
    return os.environ.get(DRY_RUN_ENV, "").strip().lower() in {"1", "true", "yes"}


def run_classify(court: str | None = None, *, dry_run_only: bool | None = None) -> RouterStats:
    """``jg classify``: label every unlabelled citation edge. Cheapest tier first.

    Structural and triage rows are written first and are always written, so M3's "treatment
    has rows" holds with no API key present at all. The escalated edges then go to the
    reasoning model — and if ``ANTHROPIC_API_KEY`` is missing the run says so in Czech,
    prints the tier report and stops, which is exactly the dry run. Nothing crashes and
    nothing is left half-written.
    """
    console = Console()
    dry = _dry_run_requested() if dry_run_only is None else dry_run_only

    with connect() as conn:
        edges = load_edges(conn, court)
        routings, stats = route_all(edges)
        missing_verdicts = sum(1 for edge in edges if not edge.verdict_text)

        def report(title: str = "Směrování klasifikace", note: str | None = None) -> RouterStats:
            console.print(stats_table(stats, title=title))
            if missing_verdicts:
                console.print(
                    f"[yellow]Bez výroku:[/yellow] {missing_verdicts} z {len(edges)} hran nemá "
                    "v cache uložený výrok citujícího rozhodnutí, takže u nich nelze zjistit "
                    "QUASHED."
                )
            if note:
                console.print(note)
            return stats

        if dry:
            return report(
                title="Směrování klasifikace (nasucho)",
                note=f"[yellow]Nasucho[/yellow] ({DRY_RUN_ENV}): nic nezapsáno, "
                "žádné volání modelu.",
            )

        written = 0
        escalations: list[Escalation] = []
        for routing in routings:
            if isinstance(routing, Escalation):
                escalations.append(routing)
            else:
                written += write_treatment(conn, routing)
        log.info("classify: %d edges, %d rows written without a model call", len(edges), written)

        if not escalations:
            return report()

        if llm_api_key() is None:
            return report(
                note=f"[yellow]ANTHROPIC_API_KEY není nastaven[/yellow]: "
                f"{len(escalations)} hran čeká na model. Strukturální a triage vrstva jsou "
                "zapsané, nic dalšího se nespustilo."
            )
        try:
            model = anthropic_model()
        except ModelUnavailable as exc:
            return report(note=f"[red]Model není k dispozici:[/red] {exc}")

        cache = DbResponseCache(conn)
        for escalation in escalations:
            result = classify_edge(escalation.edge, model=model, cache=cache)
            write_treatment(conn, result)
            stats.record_reasoning(result)
        return report()

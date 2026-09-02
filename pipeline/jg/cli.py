"""``jg``: the pipeline entry point.

Crawl, extract, classify, status. ``jg --help`` must work in a checkout where the crawler
and the classifier do not exist yet, so those two commands import their implementation
inside the command body rather than at module import time.
"""

from __future__ import annotations

import datetime as dt
import importlib
import logging
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="jg",
    help="Judikat Guard pipeline: crawl, normalise, extract citations, classify treatments.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _version() -> str:
    try:
        return package_version("jg")
    except PackageNotFoundError:  # pragma: no cover - only when run from a non-installed tree
        return "0.0.0+unknown"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"jg {_version()}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Print version"),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Judikat Guard pipeline."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _lazy(modules: Sequence[str], names: Sequence[str]) -> Callable[..., Any]:
    """Import a sibling stage on demand.

    ``jg.crawl`` and ``jg.classify`` are written by other stages of the project and may not
    exist yet. Importing them lazily keeps ``jg --help`` and ``jg status`` working
    regardless, and turns a missing stage into a clear message plus exit code 1 rather than
    a traceback at CLI startup.
    """
    last_error: Exception | None = None
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            last_error = exc
            continue
        for name in names:
            candidate = getattr(module, name, None)
            if callable(candidate):
                return candidate
        last_error = AttributeError(
            f"{module_name} nenabízí žádnou z funkcí {', '.join(names)}"
        )
    err_console.print(
        f"[red]Tato část pipeline zatím není k dispozici:[/red] {last_error}\n"
        f"Hledáno v modulech: {', '.join(modules)}."
    )
    raise typer.Exit(1)


def _iso_date(value: str | None, flag: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        err_console.print(f"[red]{flag} musí být datum ve tvaru RRRR-MM-DD:[/red] {value!r}")
        raise typer.Exit(1) from exc


@app.command()
def crawl(
    court: Annotated[str, typer.Argument(help="Court code, e.g. NSS, NS, US")],
    since: Annotated[
        str | None, typer.Option("--since", help="First decision date, RRRR-MM-DD")
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="Last decision date, RRRR-MM-DD")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Stop after this many decisions")
    ] = None,
) -> None:
    """Crawl one court into the on-disk cache and load it into the database.

    Without --since the crawler takes a short recent window rather than reaching for the
    whole database; a bulk load should always state its window. Everything is cached under
    data/raw/, so re-running the same window issues no network requests and an interrupted
    run resumes from disk.
    """
    run = _lazy(("jg.crawl",), ("run_crawl", "crawl", "main"))
    stored = run(
        court.upper(),
        limit=limit,
        since=_iso_date(since, "--since"),
        until=_iso_date(until, "--until"),
    )
    if isinstance(stored, int):
        console.print(f"Uloženo rozhodnutí: [bold]{stored}[/bold]")


@app.command()
def extract(
    court: Annotated[
        str | None, typer.Option("--court", help="Restrict to one court code")
    ] = None,
) -> None:
    """Rules pass over the crawled corpus: find citations, resolve them, write edges."""
    from jg.db import connect
    from jg.extract import run_extract

    with connect() as conn:
        stats = run_extract(conn, court.upper() if court else None)

    table = Table(title="Extrakce citací", show_edge=False)
    table.add_column("Ukazatel")
    table.add_column("Hodnota", justify="right")
    table.add_row("Rozhodnutí", str(stats.decisions))
    table.add_row("Odstavce", str(stats.paragraphs))
    table.add_row("Nalezené odkazy", str(stats.references))
    table.add_row("Zapsané citace", str(stats.citations_written))
    table.add_row("Nově založená ustanovení", str(stats.provisions_created))
    table.add_row("Věty pro anaforický průchod", str(stats.anaphora_candidates))
    console.print(table)
    console.print(_coverage_table(stats.resolution))


def _coverage_table(resolution: Any) -> Table:
    table = Table(title="Pokrytí resoluce", show_edge=False)
    table.add_column("Druh odkazu")
    table.add_column("Vyřešeno", justify="right")
    table.add_column("Nevyřešeno", justify="right")
    table.add_column("Pokrytí", justify="right")
    for kind in resolution.kinds():
        table.add_row(
            kind,
            str(resolution.resolved[kind]),
            str(resolution.unresolved[kind]),
            f"{resolution.coverage_for(kind):.1%}",
        )
    table.add_section()
    table.add_row(
        "celkem",
        str(sum(resolution.resolved.values())),
        str(sum(resolution.unresolved.values())),
        f"{resolution.coverage:.1%}",
    )
    for basis, count in sorted(resolution.basis.items()):
        table.add_row(f"základ: {basis}", str(count), "", "")
    return table


@app.command()
def classify(
    court: Annotated[
        str | None, typer.Option("--court", help="Restrict to one court code")
    ] = None,
) -> None:
    """Label every citation edge with a treatment. Structural first, model only where needed."""
    run = _lazy(("jg.classify.router", "jg.classify"), ("run_classify", "classify", "main"))
    run(court.upper() if court else None)


@app.command()
def status() -> None:
    """Per-court row counts and corpus coverage."""
    import psycopg

    from jg.db import connect

    try:
        with connect() as conn:
            courts = conn.execute(
                """
                select c.code,
                       c.name,
                       count(d.ecli)     as decisions,
                       max(d.decided_on) as latest,
                       m.decision_count  as meta_count,
                       m.covered_through as covered_through,
                       m.refreshed_at    as refreshed_at
                  from court c
                  left join decision d    on d.court_code = c.code
                  left join corpus_meta m on m.court_code = c.code
                 group by c.code, c.name, m.decision_count, m.covered_through, m.refreshed_at
                 order by c.code
                """
            ).fetchall()
            totals = conn.execute(
                """
                select (select count(*) from decision_paragraph) as paragraphs,
                       (select count(*) from decision_alias)     as aliases,
                       (select count(*) from provision)          as provisions,
                       (select count(*) from citation)           as citations,
                       (select count(cited_ecli) from citation)  as to_decision,
                       (select count(cited_provision) from citation) as to_provision,
                       (select count(*) from treatment)          as treatments
                """
            ).fetchone()
            labels = conn.execute(
                "select label, count(*) as n from treatment group by label order by label"
            ).fetchall()
    except psycopg.OperationalError as exc:
        err_console.print(f"[red]Databáze není dostupná:[/red] {exc}")
        raise typer.Exit(1) from exc

    per_court = Table(title="Korpus podle soudů", show_edge=False)
    per_court.add_column("Soud")
    per_court.add_column("Název")
    per_court.add_column("Rozhodnutí", justify="right")
    per_court.add_column("Nejnovější")
    per_court.add_column("Pokrytí do")
    per_court.add_column("Aktualizováno")
    for row in courts:
        per_court.add_row(
            row["code"],
            row["name"],
            str(row["decisions"]),
            _date(row["latest"]),
            _date(row["covered_through"]),
            _date(row["refreshed_at"]),
        )
    console.print(per_court)

    if totals is None:  # pragma: no cover - the scalar subquery form always returns a row
        return

    overall = Table(title="Řádky celkem", show_edge=False)
    overall.add_column("Tabulka")
    overall.add_column("Řádků", justify="right")
    overall.add_row("decision_paragraph", str(totals["paragraphs"]))
    overall.add_row("decision_alias", str(totals["aliases"]))
    overall.add_row("provision", str(totals["provisions"]))
    overall.add_row("citation", str(totals["citations"]))
    overall.add_row("  z toho na rozhodnutí", str(totals["to_decision"]))
    overall.add_row("  z toho na ustanovení", str(totals["to_provision"]))
    overall.add_row("treatment", str(totals["treatments"]))
    for row in labels:
        overall.add_row(f"  {row['label']}", str(row["n"]))
    console.print(overall)

    if not totals["citations"]:
        console.print("[yellow]Zatím žádné citace. Spusťte `jg extract`.[/yellow]")


def _date(value: object) -> str:
    return "—" if value is None else str(value)[:19]


def _mount_provisions() -> None:
    """Expose the M6 provision stage as ``jg provisions ...``.

    Guarded for the same reason ``crawl`` and ``classify`` import lazily: ``jg --help`` and
    ``jg status`` must keep working in a checkout where a stage is missing or mid-edit. A
    failure here registers a command that explains itself instead of breaking the whole CLI.
    """
    try:
        from jg.provisions import _app as provisions_app
    except ImportError as exc:  # pragma: no cover - only in a partial checkout
        message = str(exc)

        @app.command("provisions")
        def provisions_unavailable() -> None:
            """Provision ingest (nedostupné v tomto checkoutu)."""
            err_console.print(f"[red]Vrstva ustanovení není k dispozici:[/red] {message}")
            raise typer.Exit(1)

        return

    app.add_typer(
        provisions_app(),
        name="provisions",
        help="Ustanovení: ingest e-Sbírky, verze účinné k datu, posouzení podstatnosti změny.",
    )


_mount_provisions()

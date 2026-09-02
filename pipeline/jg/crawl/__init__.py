"""Crawlers for the four allowlisted sources. PLAN.md section 5, milestone M1.

Every court module exposes the same three things so ``jg crawl <COURT>`` can dispatch on
a string:

``COURT_CODE``
    The ``court.code`` value the normaliser writes. ``esbirka`` is not a court and uses
    ``SOURCE_CODE`` instead.
``BASE_URL``
    The site root, always on an allowlisted host.
``crawl(fetcher, limit=None, since=None, until=None)``
    An iterator of :class:`jg.models.RawDecision` (provision records for ``esbirka``).

Parsing is kept in pure functions that take HTML text, so they are testable off-line and
so a cached page in ``data/raw/`` can be re-parsed without touching the network.

Coverage as of this commit, stated plainly because a crawler that pretends to work is
worse than one that refuses:

===========  =================================================================
``nssoud``   **Working.** Searches ``vyhledavac.nssoud.cz`` day by day in the
             ECLI result view, so every ECLI is read off the page rather than
             derived from a case number. Full text, ``[N]`` paragraph numbering
             and panel type are all real. The host was added to the allowlist
             on explicit authorisation; see PLAN.md section 18. One stated
             limit: the site's paging is not deterministic on busy days, so
             ``CrawlReport.short_days`` records the shortfall rather than
             hiding it.
``nalus``    Decision-text parser written against the live
             ``GetText.aspx`` markup. Enumeration is a seam: NALUS search is an
             ASP.NET postback with session state.
``nsoud``    Panel detection is real. ``crawl()`` refuses:
             ``rozhodnuti.nsoud.cz/robots.txt`` is ``Disallow: /`` for every
             user agent but the ministry's own crawler. Not workable without
             the court's permission.
``esbirka``  Local dump reader and version-timeline builder are real. ``crawl()``
             refuses: ``opendata.eselpoint.cz`` answers *every* path, including a
             deliberately nonsense control path, with one byte-identical notice
             that the service moved to ``e-sbirka.gov.cz``, which nobody has
             allowlisted.
===========  =================================================================
"""

from __future__ import annotations

import datetime as dt
import inspect
import logging
from types import ModuleType

from jg.crawl import esbirka, nalus, nsoud, nssoud
from jg.crawl.base import (
    CachedPage,
    CrawlUnavailable,
    Fetcher,
    FetchError,
    FetchFailed,
    HostNotAllowlisted,
    ParserSeam,
    RobotsDisallowed,
)

__all__ = [
    "SOURCES",
    "CachedPage",
    "CrawlUnavailable",
    "FetchError",
    "FetchFailed",
    "Fetcher",
    "HostNotAllowlisted",
    "ParserSeam",
    "RobotsDisallowed",
    "run_crawl",
]

log = logging.getLogger(__name__)

#: ``jg crawl <CODE>`` dispatch table. Keys are the ``court.code`` values seeded by
#: ``V1__init.sql`` (plus the diacritic spelling a Czech keyboard produces for ÚS, and
#: ``ESBIRKA``, which is a statute source rather than a court).
SOURCES: dict[str, ModuleType] = {
    "NSS": nssoud,
    "NS": nsoud,
    "US": nalus,
    "ÚS": nalus,
    "ESBIRKA": esbirka,
    "E-SBIRKA": esbirka,
}


def run_crawl(
    court: str,
    *,
    limit: int | None = None,
    since: dt.date | None = None,
    until: dt.date | None = None,
) -> int:
    """``jg crawl <COURT>``: crawl one source into ``data/raw/`` and store what it yields.

    Returns the number of decisions written. A source that cannot be crawled under this
    project's own rules (not allowlisted, robots.txt disallows it, the open-data host was
    retired) raises ``SystemExit`` carrying that source's own explanation, so the CLI
    reports something the operator can escalate instead of a traceback.

    The database work is imported lazily: ``jg.crawl`` stays importable — and the
    panel-detection tests stay DB-free — in a checkout with no Postgres running.
    """
    code = court.strip().upper()
    module = SOURCES.get(code)
    if module is None:
        raise SystemExit(
            f"unknown source {court!r}. Known: {', '.join(sorted(SOURCES))}. "
            "A new source means a new allowlisted host, which CLAUDE.md rule 6 says to "
            "raise with a human first."
        )

    if module is esbirka:
        # e-Sbírka yields provision versions, not decisions (PLAN.md section 10), so it goes
        # to the provision loader rather than the decision normaliser. The remote source is
        # currently retired, so `crawl` states that; the local dumps still ingest.
        from jg.db import connect as connect_db
        from jg.provisions import ingest_dumps

        try:
            for _record in esbirka.crawl(Fetcher(esbirka.SOURCE_CODE), limit=limit, since=since):
                pass
        except CrawlUnavailable as exc:
            raise SystemExit(str(exc)) from exc
        with connect_db() as conn:
            stats = ingest_dumps(conn, limit=limit)
        log.info(
            "e-Sbírka: %d records over %d dump file(s)", stats.records, len(stats.files)
        )
        return stats.versions_inserted + stats.versions_updated

    from jg.db import connect
    from jg.normalize import store_decisions

    # Only nssoud takes a closed [since, until] window; nalus and nsoud take (limit, since).
    # Decided by inspecting the signature rather than by catching TypeError, which would
    # also swallow a genuine TypeError raised from inside the generator body.
    window: dict[str, dt.date | None] = {}
    if "until" in inspect.signature(module.crawl).parameters:
        window["until"] = until
    elif until is not None:
        log.warning("%s.crawl takes no until=; ignoring --until", module.__name__)

    with Fetcher(module.COURT_CODE) as fetcher:
        try:
            decisions = module.crawl(fetcher, limit=limit, since=since, **window)
            with connect() as conn:
                stored = store_decisions(decisions, conn)
        except CrawlUnavailable as exc:
            raise SystemExit(str(exc)) from exc

    log.info("%s: stored %d decisions", module.COURT_CODE, stored)
    return stored

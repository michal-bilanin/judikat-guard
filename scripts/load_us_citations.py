"""Load the Constitutional Court decisions the crawled corpus actually cites.

PLAN.md section 18, "The ÚS corpus: enumeration is not the blocker, the primary key is".

NALUS cannot be enumerated — its search is a stateful ASP.NET postback — but it does not
need to be. The full-text URL is addressed by a document key derived from the case number,
and the corpus tells us exactly which ÚS case numbers matter. So this script:

1. scans ``decision_paragraph`` for ÚS case numbers with the shared ``case_no_us`` pattern
   from ``extract/patterns.toml`` (the same regex the Java runtime uses),
2. folds them onto NALUS document keys, most-cited first, so a ``--limit`` run buys the
   most resolution per request,
3. fetches each one through the ordinary :class:`jg.crawl.base.Fetcher` — allowlist,
   robots, one request per second and the ``data/raw/US/`` cache all come for free — and
4. stores it with :func:`jg.normalize.store_decision`, plus the extra case-number alias
   spellings that make the cited references resolve.

Why this unlocks a red light: an ÚS **výrok** annuls a named NSS decision, which is the
structural ``QUASHED`` rule of PLAN.md section 8 — a RED light that needs no model call.

    python scripts/load_us_citations.py --limit 40
    make load-us LIMIT=40

Resumability. Successful pages replay from ``data/raw/US/`` and issue zero network
requests; decisions already in the database are skipped outright. Failures are remembered
too, in :data:`MISS_LEDGER`, because a 404 is never written to the HTTP cache and a re-run
would otherwise re-ask NALUS the same dead question every time. ``--retry-misses`` clears
that memory deliberately.

Nothing here invents an identifier (CLAUDE.md rule 1). The primary key is the NALUS
document key the page is addressed by; the case number, date and SbNU citation are read off
the page that was fetched; and a page whose printed registry sign does not match the case
number that was asked for is **reported and skipped**, never stored.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

from jg import config
from jg.classify.structural import fold
from jg.crawl import nalus
from jg.crawl.base import Fetcher, FetchError
from jg.db import Conn, connect
from jg.extract.patterns import markers, pattern_set
from jg.extract.resolver import normalize_alias
from jg.models import AliasKind, RawDecision
from jg.normalize import refresh_corpus_meta, store_decision

log = logging.getLogger("load_us_citations")

#: Failures remembered across runs. A 404 or an unparsable page never reaches the HTTP
#: cache, so without this a re-run re-requests every dead key — which is the spirit of
#: CLAUDE.md rule 5 even though the letter of it only covers pages that were fetched.
MISS_LEDGER = config.DATA_DIR / "us_citations_misses.json"

#: Cheap SQL prefilter before the shared regex runs. 10,807 of 433,564 paragraphs contain
#: "ÚS" at all, so this is the difference between a half-second scan and a minute of one.
_SELECT_PARAGRAPHS = """
select body
  from decision_paragraph
 where body like %s
"""

_SELECT_STORED_KEYS = """
select ecli
  from decision
 where court_code = %s
"""

# Same statement as jg.normalize's alias upsert. The extra spellings below are rows, not
# schema: CLAUDE.md rule 4 keeps every `create` and `alter` in Flyway.
_UPSERT_ALIAS = """
insert into decision_alias (alias, alias_kind, ecli)
values (%s, %s, %s)
on conflict (alias) do nothing
"""


@dataclass
class Target:
    """One NALUS document key and every way the corpus spells the case behind it."""

    document_key: str
    #: Cited spellings, most frequent first. The first is used to address the fetch.
    spellings: list[str] = field(default_factory=list)
    mentions: int = 0

    @property
    def case_no(self) -> str:
        return self.spellings[0]


@dataclass
class LoadReport:
    """What one run did. Printed in full; nothing is swallowed."""

    targets: int = 0
    skipped_already_stored: int = 0
    skipped_known_miss: int = 0
    stored: int = 0
    from_cache: int = 0
    aliases_written: int = 0
    quashing_verdicts: int = 0
    missing_verdict: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def reasons(self) -> Counter[str]:
        return Counter(reason.split(":", 1)[0] for reason in self.failures.values())


# ------------------------------------------------------------------ scanning the corpus


def scan_targets(conn: Conn, *, like: str = "%ÚS%") -> list[Target]:
    """Every distinct ÚS case number in ``decision_paragraph``, folded onto document keys.

    Uses ``case_no_us`` from ``extract/patterns.toml`` rather than a regex of its own, so
    what gets loaded is exactly what the extractor will later try to resolve.

    Two spellings of the filing year reach the same key on purpose: the corpus cites the
    same decision as both ``Pl. ÚS 44/2021`` and ``Pl. ÚS 44/21``, and NALUS answers
    ``Pl-44-21_1``. :func:`jg.crawl.nalus.matches_sign` checks that fold against the page
    before anything is stored.
    """
    pattern = pattern_set().by_name("case_no_us")
    counts: dict[str, Counter[str]] = {}
    with conn.cursor() as cur:
        cur.execute(_SELECT_PARAGRAPHS, (like,))
        for row in cur:
            for match in pattern.regex.finditer(row["body"]):
                case_no = " ".join(match.group(1).split())
                key = nalus.nalus_sz(case_no)
                if key is None:  # pragma: no cover - the pattern guarantees a match
                    continue
                counts.setdefault(key, Counter())[case_no] += 1

    targets = [
        Target(
            document_key=key,
            spellings=[spelling for spelling, _n in spellings.most_common()],
            mentions=sum(spellings.values()),
        )
        for key, spellings in counts.items()
    ]
    # Most-cited first, so a bounded run buys the most citation resolution per request.
    targets.sort(key=lambda t: (-t.mentions, t.document_key))
    return targets


def stored_keys(conn: Conn) -> set[str]:
    """Document keys already in ``decision``, so a resumed run skips them."""
    prefix = f"{nalus.ECLI_NAMESPACE}:"
    return {
        row["ecli"].removeprefix(prefix)
        for row in conn.execute(_SELECT_STORED_KEYS, (nalus.COURT_CODE,)).fetchall()
        if row["ecli"].startswith(prefix)
    }


# ------------------------------------------------------------------------- the ledger


def read_misses() -> dict[str, str]:
    if not MISS_LEDGER.exists():
        return {}
    try:
        raw = json.loads(MISS_LEDGER.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("%s is unreadable; treating every key as untried", MISS_LEDGER)
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def write_misses(misses: dict[str, str]) -> None:
    MISS_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    MISS_LEDGER.write_text(
        json.dumps(dict(sorted(misses.items())), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# -------------------------------------------------------------------------- the write


def extra_aliases(raw: RawDecision, target: Target) -> list[tuple[str, str, str]]:
    """Alias rows beyond the ones :func:`jg.normalize.alias_rows` already writes.

    ``store_decision`` writes one ``case_no`` alias, for the sign exactly as NALUS printed
    it (``IV.ÚS 3523/20``). Citing decisions write ``IV. ÚS 3523/20``, with the space, so
    without the second spelling the 14,711 ÚS mentions in the corpus resolve to nothing.

    Every cited spelling that folded onto this document key is written too, but only after
    the fetched page's own registry sign confirmed the match — which is why this function
    takes the parsed decision rather than only the target. Those strings are corpus data
    confirmed against the source, not identifiers manufactured here.
    """
    if not raw.case_no:
        return []
    wanted = list(nalus.alias_spellings(raw.case_no))
    wanted.extend(
        spelling for spelling in target.spellings if nalus.matches_sign(spelling, raw.case_no)
    )
    rows: dict[str, tuple[str, str, str]] = {}
    for spelling in wanted:
        alias = normalize_alias(spelling)
        if alias:
            rows.setdefault(alias, (alias, str(AliasKind.CASE_NO), raw.ecli))
    return list(rows.values())


def store(conn: Conn, raw: RawDecision, target: Target) -> int:
    """Persist one ÚS decision and its alias spellings. Returns aliases written."""
    store_decision(raw, conn)
    rows = extra_aliases(raw, target)
    written = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(_UPSERT_ALIAS, row)
            written += cur.rowcount
    return written


def load_one(fetcher: Fetcher, conn: Conn, target: Target, report: LoadReport) -> bool:
    """Fetch, verify and store one target. Returns True when a row was written."""
    cached = fetcher.is_cached(nalus.text_url(target.document_key))
    try:
        raw = nalus.fetch_decision(fetcher, target.case_no)
    except nalus.EmptyDocument as exc:
        # NALUS answered 200 with the page chrome and nothing in it. A coverage fact about
        # the source, not a bug here, so it is counted separately in the report.
        report.failures[target.document_key] = f"empty-document:{exc}"
        return False
    except FetchError as exc:
        report.failures[target.document_key] = f"fetch:{exc}"
        return False
    except ValueError as exc:
        report.failures[target.document_key] = f"parse:{exc}"
        return False

    if not nalus.matches_sign(target.case_no, raw.case_no):
        # The one guard that makes the two-digit-year fold safe. Refusing here costs one
        # decision; storing a page under the wrong case number would put a red light on a
        # decision that was never annulled.
        report.failures[target.document_key] = (
            f"sign-mismatch:asked for {target.case_no!r}, page says {raw.case_no!r}"
        )
        return False

    report.aliases_written += store(conn, raw, target)
    report.stored += 1
    report.from_cache += int(cached)
    if not raw.verdict_text:
        report.missing_verdict.append(raw.ecli)
    else:
        # Same vocabulary the structural classifier uses, read from the same TOML, so this
        # count is a preview of the QUASHED candidates rather than a second opinion.
        folded = fold(raw.verdict_text)
        if any(fold(marker) in folded for marker in markers("quashing")):
            report.quashing_verdicts += 1
    return True


# ------------------------------------------------------------------------------ driver


def run(
    *,
    limit: int | None,
    every: int,
    retry_misses: bool,
    dry_run: bool,
    cached_only: bool,
) -> LoadReport:
    report = LoadReport()
    misses = {} if retry_misses else read_misses()
    started = time.time()

    with connect() as conn:
        targets = scan_targets(conn)
        already = stored_keys(conn)
        print(
            f"corpus cites {len(targets)} distinct ÚS case numbers "
            f"({sum(t.mentions for t in targets)} mentions); "
            f"{len(already)} already stored, {len(misses)} known misses",
            flush=True,
        )

        pending = [t for t in targets if t.document_key not in already]
        report.skipped_already_stored = len(targets) - len(pending)
        if not retry_misses:
            before = len(pending)
            pending = [t for t in pending if t.document_key not in misses]
            report.skipped_known_miss = before - len(pending)

        if dry_run:
            report.targets = len(pending)
            for target in pending[: limit or 20]:
                print(f"  would fetch {target.document_key:<14} {target.case_no:<18} "
                      f"{target.mentions:5d} mentions")
            return report

        with Fetcher(nalus.COURT_CODE) as fetcher:
            for target in pending:
                if limit is not None and report.targets >= limit:
                    break
                url = nalus.text_url(target.document_key)
                if cached_only and not fetcher.is_cached(url):
                    continue
                report.targets += 1
                if load_one(fetcher, conn, target, report):
                    misses.pop(target.document_key, None)
                else:
                    misses[target.document_key] = report.failures[target.document_key]
                if report.targets % every == 0:
                    conn.commit()
                    write_misses(misses)
                    print(
                        f"[{(time.time() - started) / 60:6.1f}m] tried={report.targets:5d} "
                        f"stored={report.stored:5d} cache={report.from_cache:5d} "
                        f"failed={len(report.failures):4d} last={target.case_no}",
                        flush=True,
                    )
        conn.commit()
        refresh_corpus_meta(conn)

    write_misses(misses)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None, help="stop after N fetch attempts")
    parser.add_argument("--every", type=int, default=25, help="commit/report interval")
    parser.add_argument(
        "--retry-misses", action="store_true", help=f"ignore {MISS_LEDGER.name} and re-ask"
    )
    parser.add_argument(
        "--cached-only",
        action="store_true",
        help="store only what is already in data/raw/US; makes zero network requests",
    )
    parser.add_argument("--dry-run", action="store_true", help="list targets, fetch nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(message)s", stream=sys.stdout
    )

    report = run(
        limit=args.limit,
        every=args.every,
        retry_misses=args.retry_misses,
        dry_run=args.dry_run,
        cached_only=args.cached_only,
    )

    print(f"\n=== ÚS load, {dt.date.today().isoformat()} ===")
    print(f"attempted            : {report.targets}")
    print(f"stored               : {report.stored}")
    print(f"  replayed from cache: {report.from_cache}")
    print(f"extra aliases written: {report.aliases_written}")
    print(f"skipped, already in db: {report.skipped_already_stored}")
    print(f"skipped, known miss  : {report.skipped_known_miss}")
    print(f"výrok carries a quashing marker: {report.quashing_verdicts}")
    print(f"no výrok parsed      : {len(report.missing_verdict)} {report.missing_verdict[:5]}")
    print(f"failed               : {len(report.failures)} {dict(report.reasons)}")
    for key, reason in list(report.failures.items())[:10]:
        print(f"  {key:<14} {reason[:110]}")
    if len(report.failures) > 10:
        print(f"  ... and {len(report.failures) - 10} more, all in {MISS_LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

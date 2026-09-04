"""Bulk NSS corpus load over a date window (milestone M1).

Runs at one request per second per host (CLAUDE.md rule 5), so a full year takes over an
hour. Every request goes through the on-disk cache in ``data/raw/``, so an interrupted run
resumes from disk and a repeat run issues no network requests at all.

    python scripts/bulk_crawl.py 2023-01-01 2023-12-31

The day-by-day chunking, and the re-walk of days whose paging loses rows, live in
``jg.crawl.nssoud``; this script only drives it and reports what it saw. Coverage shortfalls
are printed rather than swallowed, because the corpus bound is what the product quotes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time

from jg.crawl import nssoud
from jg.crawl.base import Fetcher
from jg.db import connect
from jg.normalize import refresh_corpus_meta, store_decision


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl NSS decisions in [since, until].")
    parser.add_argument("since", type=dt.date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("until", type=dt.date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--every", type=int, default=25, help="commit/report interval")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(message)s", stream=sys.stdout
    )

    report = nssoud.CrawlReport()
    started = time.time()
    stored = 0

    with Fetcher("NSS") as fetcher, connect() as conn:
        for raw in nssoud.crawl(fetcher, since=args.since, until=args.until, report=report):
            store_decision(raw, conn)
            stored += 1
            if stored % args.every == 0:
                conn.commit()
                print(
                    f"[{(time.time() - started) / 60:6.1f}m] stored={stored:6d} "
                    f"days={report.days:4d} nss={report.nss_rows:6d} "
                    f"other={report.other_court_rows:6d} at={raw.decided_on}",
                    flush=True,
                )
        conn.commit()
        meta = refresh_corpus_meta(conn)
        conn.commit()

    minutes = (time.time() - started) / 60
    print(f"\n=== DONE {args.since}..{args.until} in {minutes:.1f} min ===")
    print(f"stored              : {stored}")
    print(f"days walked         : {report.days}")
    print(f"reported total      : {report.reported_total} (all courts)")
    print(f"distinct rows seen  : {report.rows_seen}")
    print(f"NSS rows            : {report.nss_rows}")
    print(f"other-court rows    : {report.other_court_rows}")
    print(f"unnumbered paragraphs: {report.unnumbered}")
    print(f"days short on paging: {len(report.short_days)}")
    print(f"corpus_meta         : {meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

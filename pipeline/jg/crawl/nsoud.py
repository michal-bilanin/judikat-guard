"""Nejvyšší soud (NS), ``rozhodnuti.nsoud.cz``. PLAN.md section 5.

**This source is closed to us and the blocker is not technical.**
``https://rozhodnuti.nsoud.cz/robots.txt`` reads, in full effect:

    User-agent: *
    Disallow: /

    User-agent: DG_JUSTICE_CRAWLER
    Allow: /Judikatura/judikatura_ns.nsf/

The court allows exactly one crawler, the Ministry of Justice's own, and forbids
everyone else every path. CLAUDE.md rule 5 says honour robots.txt, so
:class:`~jg.crawl.base.Fetcher` refuses every URL on this host with
:class:`~jg.crawl.base.RobotsDisallowed` and :func:`crawl` refuses before it even gets
there. Impersonating ``DG_JUSTICE_CRAWLER`` would be evading the rule, not honouring it.

The robots file does advertise dated sitemaps under ``/<yyyy>/<mm>/<dd>/sitemap_<n>.xml``,
which is how the corpus would be enumerated if the court ever granted access — but they
are inside the ``Disallow``, so they were not fetched and no parser is written against
markup nobody has seen.

:func:`panel_type` is real and needs no network: *velký senát* is NS's answer to the NSS
extended panel (PLAN.md section 1, §20 ZSS) and drives the M3 structural signal.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator

from jg.crawl.base import CrawlUnavailable, Fetcher, ParserSeam, detect_marker
from jg.models import PanelType, RawDecision

COURT_CODE = "NS"
BASE_URL = "https://rozhodnuti.nsoud.cz/"

#: The only user agent this host's robots.txt permits. We are not it.
PERMITTED_USER_AGENT = "DG_JUSTICE_CRAWLER"


# ``velký senát`` in any case ending, on folded (diacritic-free, lowercased) text. Covers
# "velký senát trestního kolegia" and "velkého senátu občanskoprávního a obchodního
# kolegia" alike.
_GRAND_MARKERS = (re.compile(r"\bvelk\w* senat\w*\b"),)

_REFERRAL_VERBS = re.compile(
    r"\b(?:postoup\w*|postupuje|postupuji|predlozi\w*|predlozen\w*|predklad\w*|"
    r"prikaz\w*|navrh\w* na postoupeni)\b"
)


def panel_type(
    *,
    heading: str | None = None,
    case_no: str | None = None,
    decision_kind: str | None = None,
) -> PanelType:
    """Classify the panel that issued an NS decision. Pure.

    Feed it metadata or the heading, never the body. A three-judge senate that *refers* a
    question to the grand panel is still a three-judge senate, so a referral phrasing
    ("postoupil věc velkému senátu") yields ``PANEL``; treating it as ``GRAND`` would hand
    the decision authority it does not have and turn an amber conflict into a false red.

    ``UNKNOWN`` only when nothing was supplied.
    """
    fields = [f for f in (heading, case_no, decision_kind) if f and f.strip()]
    if not fields:
        return PanelType.UNKNOWN
    haystack = " \n ".join(fields)
    if detect_marker(haystack, _GRAND_MARKERS, referral_verbs=_REFERRAL_VERBS):
        return PanelType.GRAND
    return PanelType.PANEL


_PARSER_SEAM = (
    "jg.crawl.nsoud has no verified decision-page parser. rozhodnuti.nsoud.cz robots.txt "
    "is 'Disallow: /' for every user agent except the ministry's own crawler, so no page "
    "was ever fetched and every selector would be a guess. If access is granted: parse "
    "ecli, case_no (sp. zn.), decided_on, journal_no (R-číslo) and the výrok, take "
    "paragraphs from base.text_paragraphs, and pass only the heading to panel_type()."
)


def parse_decision(html: str, *, source_url: str, fetched_at: dt.datetime) -> RawDecision:
    """Seam. Raises :class:`ParserSeam`; see the module docstring for why."""
    raise ParserSeam(_PARSER_SEAM)


def crawl(
    fetcher: Fetcher,
    limit: int | None = None,
    since: dt.date | None = None,
) -> Iterator[RawDecision]:
    """Refuses, eagerly. robots.txt disallows every path on this host for our user agent."""
    raise CrawlUnavailable(
        f"NS cannot be crawled: {BASE_URL}robots.txt is 'Disallow: /' for 'User-agent: *' "
        f"and allows only {PERMITTED_USER_AGENT}. CLAUDE.md rule 5 says honour robots.txt, "
        "so this stays refused until the court grants access; do not change the user agent "
        "to work around it."
    )

"""Ústavní soud (ÚS) via NALUS, ``nalus.usoud.cz``. PLAN.md section 5.

What was observed against the live site (``robots.txt`` is 404 there, so nothing is
disallowed; the delay is still one request per second):

* ``Search/GetText.aspx?sz=<sz>`` renders one decision in full, **without a session**.
  That is the whole reason this module has a real parser while its siblings do not.
  Metadata sits in four labelled spans and the text in ``td.DocContent``, with
  ``<br/><br/>`` as the paragraph separator.
* ``Search/Search.aspx`` (enumeration) and ``Search/GetRegSignDecisions.aspx`` are ASP.NET
  postbacks: a GET to either bounces back to an empty search form, because the result set
  lives in ``__VIEWSTATE`` and server session state. Enumerating the corpus therefore
  needs a POST driver, which :class:`~jg.crawl.base.Fetcher` deliberately does not have.
* ``GetText.aspx`` does **not** print the ECLI. NALUS exposes ECLI as a *search field*
  and prints it on the search-result detail page, which is behind the same postback. So
  :func:`to_raw_decision` takes the ECLI as an argument instead of deriving one: an ECLI
  is an identifier, and CLAUDE.md rule 1 says identifiers are never manufactured.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator

from pydantic import BaseModel
from selectolax.parser import HTMLParser

from jg.crawl.base import (
    CrawlUnavailable,
    Fetcher,
    detect_marker,
    fold_text,
    parse_cs_date,
    text_paragraphs,
)
from jg.models import PanelType, RawDecision

COURT_CODE = "US"
BASE_URL = "https://nalus.usoud.cz/"

SEARCH_URL = BASE_URL + "Search/Search.aspx"
TEXT_URL_TEMPLATE = BASE_URL + "Search/GetText.aspx?sz={sz}"

#: The single ``<td>`` holding the whole decision body.
CONTENT_SELECTOR = "td.DocContent"

_LABEL_SELECTORS = {
    "registry_sign": "#lblRegistrySign",
    "parallel_citation": "#lblParallelQuotation",
    "popular_name": "#lblPopularName",
    "decision_form": "#lblDecisionForm",
}

# "II.ÚS 2379/08 ze dne 9. 7. 2009"  ->  sign, date
_REGISTRY_SIGN_RE = re.compile(
    r"^(?P<sign>.+?)\s+ze\s+dne\s+(?P<date>\d{1,2}\.\s*\d{1,2}\.\s*\d{4})"
)

# "II.ÚS 2379/08" / "II. ÚS 2379/08" / "Pl. ÚS 29/98"
# "Pl." replaces the roman numeral rather than prefixing it, hence the alternation. Same
# shape as case_no_us in extract/patterns.toml; keep the two in step.
_SIGN_PARTS_RE = re.compile(r"^\s*(?:(Pl)\.|([IVX]+)\.?)\s*ÚS\s+(\d+)/(\d{2,4})")

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}

_PLENARY_SIGN_RE = re.compile(r"^\s*pl\.?\s*us\b")
_PLENARY_MARKERS = (re.compile(r"\bplen\w*\b"),)
_REFERRAL_VERBS = re.compile(
    r"\b(?:postoup\w*|postupuje|predlozi\w*|predlozen\w*|predklad\w*|navrh\w* na postoupeni)\b"
)


class NalusDocument(BaseModel):
    """Everything ``GetText.aspx`` actually shows, before anything is assumed.

    Kept separate from :class:`jg.models.RawDecision` because the two do not line up:
    NALUS has no ECLI on this page, and it has a ``popular_name`` for which the fixed
    ``RawDecision`` contract has no column.
    """

    registry_sign: str | None = None
    decided_on: dt.date | None = None
    #: SbNU parallel citation, e.g. "N 157/54 SbNU 33". The ÚS analogue of an R-číslo.
    parallel_citation: str | None = None
    popular_name: str | None = None
    #: NÁLEZ / USNESENÍ / STANOVISKO, as printed.
    decision_form: str | None = None
    #: First lines of the body: court, panel composition, date, sp. zn. Panel detection
    #: reads this and nothing deeper.
    heading: str | None = None
    verdict_text: str | None = None
    paragraphs: list[str] = []


# ------------------------------------------------------------------ panel detection


def panel_type(
    *,
    registry_sign: str | None = None,
    heading: str | None = None,
    decision_form: str | None = None,
) -> PanelType:
    """Classify the ÚS body that issued a decision. Pure.

    The registry sign alone settles it in almost every case: the Constitutional Court
    numbers its panels in Roman numerals (``I.ÚS``…``IV.ÚS``) and marks the full assembly
    ``Pl. ÚS``. The heading is a second opinion for decisions reached without a sign.

    ``UNKNOWN`` only when nothing was supplied.
    """
    fields = [f for f in (registry_sign, heading, decision_form) if f and f.strip()]
    if not fields:
        return PanelType.UNKNOWN
    if registry_sign and _PLENARY_SIGN_RE.match(fold_text(registry_sign)):
        return PanelType.PLENARY
    if heading and detect_marker(heading, _PLENARY_MARKERS, referral_verbs=_REFERRAL_VERBS):
        return PanelType.PLENARY
    return PanelType.PANEL


# ---------------------------------------------------------------------- url helpers


def registry_sign_to_sz(registry_sign: str) -> str:
    """``II.ÚS 2379/08`` -> ``2-2379-08``, the ``sz`` key ``GetText.aspx`` takes.

    Verified for panel signs against the live site. Raises ``ValueError`` for ``Pl. ÚS``:
    the plenary form of ``sz`` was never observed and guessing it would fabricate a
    document key.
    """
    match = _SIGN_PARTS_RE.match(registry_sign.replace("\xa0", " ").strip())
    if match is None:
        raise ValueError(f"not a Constitutional Court registry sign: {registry_sign!r}")
    plenary, roman, sequence, year = match.groups()
    if plenary is not None:
        raise ValueError(
            f"the GetText.aspx 'sz' form for a plenary sign ({registry_sign!r}) was never "
            "observed; supply the sz value from the NALUS result page instead of guessing."
        )
    panel = _ROMAN.get(roman.upper())
    if panel is None:
        raise ValueError(f"unrecognised ÚS panel numeral {roman!r} in {registry_sign!r}")
    return f"{panel}-{sequence}-{year}"


def text_url(sz: str, ordinal: int = 1) -> str:
    """Full-text URL for a document key. ``ordinal`` is the document within the case."""
    return TEXT_URL_TEMPLATE.format(sz=f"{sz}_{ordinal}")


# --------------------------------------------------------------------------- parsing


def _label(tree: HTMLParser, selector: str) -> str | None:
    node = tree.css_first(selector)
    if node is None:
        return None
    value = node.text(deep=True).replace("\xa0", " ").strip()
    return value or None


def _verdict_text(tree: HTMLParser, paragraphs: list[str]) -> str | None:
    """The *výrok*, the operative part. Only this feeds the structural QUASHED rule."""
    content = tree.css_first(CONTENT_SELECTOR)
    if content is not None:
        for bold in content.css("b"):
            inner = bold.html
            if not inner:
                continue
            bold_paragraphs = text_paragraphs(inner)
            if bold_paragraphs and fold_text(bold_paragraphs[0]).startswith("vyrok"):
                body = "\n".join(bold_paragraphs[1:]).strip()
                return body or None
    # Fallback for documents that do not bold the operative part.
    for index, paragraph in enumerate(paragraphs):
        if fold_text(paragraph) == "vyrok":
            tail: list[str] = []
            for following in paragraphs[index + 1 :]:
                if fold_text(following).startswith("oduvodneni"):
                    break
                tail.append(following)
            body = "\n".join(tail).strip()
            return body or None
    return None


def parse_gettext(html: str) -> NalusDocument:
    """Parse one ``GetText.aspx`` page. Pure, off-line, no network."""
    tree = HTMLParser(html)
    labels = {name: _label(tree, sel) for name, sel in _LABEL_SELECTORS.items()}
    registry_sign = labels["registry_sign"]
    sign: str | None = registry_sign
    decided_on: dt.date | None = None
    if registry_sign:
        match = _REGISTRY_SIGN_RE.match(registry_sign)
        if match:
            sign = match.group("sign").strip()
            decided_on = parse_cs_date(match.group("date"))
    paragraphs = text_paragraphs(html, selector=CONTENT_SELECTOR)
    heading = " ".join(paragraphs[:2]) if paragraphs else None
    return NalusDocument(
        registry_sign=sign,
        decided_on=decided_on,
        parallel_citation=labels["parallel_citation"],
        popular_name=labels["popular_name"],
        decision_form=labels["decision_form"],
        heading=heading,
        verdict_text=_verdict_text(tree, paragraphs),
        paragraphs=paragraphs,
    )


def to_raw_decision(
    document: NalusDocument,
    *,
    ecli: str,
    source_url: str,
    fetched_at: dt.datetime,
) -> RawDecision:
    """Lift a parsed page into the crawl -> normalize seam.

    ``ecli`` is a required argument, not something this module derives: ``GetText.aspx``
    never prints one, and CLAUDE.md rule 1 forbids manufacturing an identifier. Take it
    from the NALUS result page, which does show it.
    """
    if document.decided_on is None:
        raise ValueError(f"no decision date parsed for {ecli}; refusing to invent one")
    return RawDecision(
        ecli=ecli,
        court_code=COURT_CODE,
        panel_type=panel_type(
            registry_sign=document.registry_sign,
            heading=document.heading,
            decision_form=document.decision_form,
        ),
        decided_on=document.decided_on,
        case_no=document.registry_sign,
        ref_no=None,
        journal_no=document.parallel_citation,
        ratio_summary=None,
        source_url=source_url,
        fetched_at=fetched_at,
        paragraphs=document.paragraphs,
        verdict_text=document.verdict_text,
    )


# --------------------------------------------------------------------------- crawling


def fetch_decision(fetcher: Fetcher, sz: str, *, ecli: str, ordinal: int = 1) -> RawDecision:
    """Fetch and parse one decision by its NALUS document key. Cache-first."""
    url = text_url(sz, ordinal)
    page = fetcher.fetch(url)
    return to_raw_decision(
        parse_gettext(page.text),
        ecli=ecli,
        source_url=url,
        fetched_at=page.fetched_at,
    )


def crawl(
    fetcher: Fetcher,
    limit: int | None = None,
    since: dt.date | None = None,
) -> Iterator[RawDecision]:
    """Refuses: NALUS enumeration is a stateful ASP.NET postback.

    Per-decision fetching works today via :func:`fetch_decision`; what is missing is the
    list of document keys and their ECLIs. Closing this needs a POST driver for
    ``Search.aspx`` that carries ``__VIEWSTATE``, ``__EVENTVALIDATION`` and the session
    cookie, and that also yields the ECLI the full-text page omits.
    """
    raise CrawlUnavailable(
        "NALUS enumeration is not implemented: a GET to "
        f"{SEARCH_URL} returns an empty search form because the result set lives in "
        "__VIEWSTATE and server session state. Use fetch_decision(fetcher, sz, ecli=...) "
        "for a known document key, or add a POST driver for Search.aspx."
    )

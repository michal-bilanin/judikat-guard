"""Ústavní soud (ÚS) via NALUS, ``nalus.usoud.cz``. PLAN.md sections 5 and 18.

This module fetches **exactly the ÚS decisions the corpus already cites**, one document
key at a time. It does not enumerate the court, and it no longer needs to: NALUS encodes
the case number in the full-text URL, and the crawled NSS corpus tells us precisely which
ÚS decisions matter. ``scripts/load_us_citations.py`` is the driver.

What was observed against the live site (``robots.txt`` is 404 there, so nothing is
disallowed; the delay is still one request per second):

* ``Search/GetText.aspx?sz=<key>`` renders one decision in full, **without a session**.
  The key is ``{panel}-{number}-{yy}_{ordinal}``: the panel is the Roman numeral mapped to
  a digit (``I``→1 … ``IV``→4) and the plenum is the literal ``Pl``. Verified live on five
  keys, all HTTP 200 with the full text: ``2-2379-08_1`` (II. ÚS 2379/08),
  ``3-989-08_1`` (III. ÚS 989/08), ``1-741-06_1`` (I. ÚS 741/06), ``Pl-44-21_1``
  (Pl. ÚS 44/21) and ``4-3523-20_1`` (IV. ÚS 3523/20).
* Metadata sits in four labelled spans (``#lblRegistrySign``, ``#lblParallelQuotation``,
  ``#lblPopularName``, ``#lblDecisionForm``) and the text in ``td.DocContent``, with
  ``<br/><br/>`` as the paragraph separator.
* ``Search/Search.aspx`` (enumeration) and ``Search/GetRegSignDecisions.aspx`` are ASP.NET
  postbacks: a GET to either bounces back to an empty search form, because the result set
  lives in ``__VIEWSTATE`` and server session state.
* ``GetText.aspx`` does **not** print an ECLI — there is no ``ECLI`` substring anywhere on
  the page — and ``ResultDetail.aspx`` 302s without a session. See :data:`ECLI_NAMESPACE`
  for what ``decision.ecli`` therefore holds for an ÚS row, and ``V5__decision_key_
  convention.sql`` for the same statement in the schema itself.

Every parse function below takes HTML text and returns plain values, so the whole parser is
testable off-line and a page cached in ``data/raw/US/`` can be re-parsed without touching
the network. ``jg.classify.router.crawl_cache_verdicts`` depends on exactly that: the
**výrok** has no column in ``decision``, so the structural ``QUASHED`` rule re-reads it off
the cached page through :func:`parse_gettext`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel
from selectolax.parser import HTMLParser

from jg.crawl.base import (
    CrawlUnavailable,
    Fetcher,
    FetchError,
    detect_marker,
    fold_text,
    parse_cs_date,
    text_paragraphs,
)
from jg.models import PanelType, RawDecision


class EmptyDocument(FetchError):
    """NALUS answered HTTP 200 with the page chrome and no decision in it.

    Observed on older case numbers (1994–2006): ``lblRegistrySign``,
    ``lblParallelQuotation`` and ``lblPopularName`` are all present but empty and
    ``td.DocContent`` holds an empty table. The record exists in the court's registry —
    ``lblDecisionForm`` still says ``USNESENÍ`` — but this document key carries no text.

    A distinct exception rather than a parse failure, because it is a fact about the
    source's coverage and belongs in the coverage report, not in a stack trace. PLAN.md
    section 16 already commits to saying where the corpus has holes.
    """

COURT_CODE = "US"
BASE_URL = "https://nalus.usoud.cz/"

SEARCH_URL = BASE_URL + "Search/Search.aspx"
TEXT_URL_TEMPLATE = BASE_URL + "Search/GetText.aspx?sz={sz}"

#: ``decision.ecli`` prefix for an ÚS row: ``nalus:4-3523-20_1``.
#:
#: NALUS publishes no ECLI (checked: no ``ECLI`` substring on ``GetText.aspx``, and the
#: crawled NSS corpus never cites one either), so there is no authoritative ÚS ECLI within
#: reach and CLAUDE.md rule 1 forbids constructing one. The primary key is therefore the
#: **NALUS document key**, namespaced so it can never be mistaken for an ECLI. That key is
#: crawled data — it is the string NALUS itself answers to — so rule 1 holds.
ECLI_NAMESPACE = "nalus"

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

# "II.ÚS 2379/08" / "II. ÚS 2379/08" / "Pl. ÚS 29/98" / "Pl. ÚS 44/2021"
# "Pl." replaces the roman numeral rather than prefixing it, hence the alternation. Same
# shape as case_no_us in extract/patterns.toml; keep the two in step.
_SIGN_PARTS_RE = re.compile(r"^\s*(?:(Pl)\.|([IVX]+)\.?)\s*ÚS\s+(\d+)/(\d{2,4})")

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}

_PLENARY_SIGN_RE = re.compile(r"^\s*pl\.?\s*us\b")
_PLENARY_MARKERS = (re.compile(r"\bplen\w*\b"),)
_REFERRAL_VERBS = re.compile(
    r"\b(?:postoup\w*|postupuje|predlozi\w*|predlozen\w*|predklad\w*|navrh\w* na postoupeni)\b"
)

#: The SbNU parallel citation, the ÚS analogue of the Supreme Court's R-číslo.
#: ``N 144/107 SbNU 211`` for a nález, ``U 12/34 SbNU 56`` for a usnesení.
_SBNU_RE = re.compile(r"\b[NU]\s+\d+/\d+\s+SbNU\s+\d+")

#: The literal marker that closes the introduction and opens the operative part. Matched
#: against a de-spaced fold, so the letter-spaced ``t a k t o :`` that some decisions print
#: is the same marker.
_VERDICT_OPENERS = ("takto:", "rozhodltakto:")
#: The older, SbNU-typeset layout labels the operative part instead of announcing it.
_VERDICT_HEADINGS = ("vyrok",)
#: What ends the operative part in both layouts.
_VERDICT_CLOSERS = ("oduvodneni",)

#: How far into the document the operative part may start. Every observed layout opens it
#: in the first few paragraphs (0 for a usnesení, 1 for a nález, 2 for the SbNU layout);
#: a "takto:" further in belongs to a quoted lower-court decision, not to this one.
_VERDICT_SEARCH_DEPTH = 6


class NalusDocument(BaseModel):
    """Everything ``GetText.aspx`` actually shows, before anything is assumed.

    Kept separate from :class:`jg.models.RawDecision` because the two do not line up:
    NALUS has no ECLI on this page, and it has a ``popular_name`` for which the fixed
    ``RawDecision`` contract has no column.
    """

    registry_sign: str | None = None
    decided_on: dt.date | None = None
    #: SbNU parallel citation, e.g. "N 157/54 SbNU 33". None for unpublished decisions,
    #: which is the normal state rather than an error.
    parallel_citation: str | None = None
    #: Every line of ``#lblParallelQuotation``, in order. A nález that also went into the
    #: Sbírka zákonů prints two, e.g. "38/2023 Sb." and "N 11/116 SbNU 87".
    parallel_citations: list[str] = []
    popular_name: str | None = None
    #: NÁLEZ / USNESENÍ / STANOVISKO, as printed.
    decision_form: str | None = None
    #: First lines of the body: court, panel composition, date, sp. zn. Panel detection
    #: reads this and nothing deeper.
    heading: str | None = None
    verdict_text: str | None = None
    paragraphs: list[str] = []


class NalusHeader(NamedTuple):
    """``(case_no, decided_on, journal_no, title)`` — the header line, as printed."""

    case_no: str | None
    decided_on: dt.date | None
    journal_no: str | None
    title: str | None


class SignParts(NamedTuple):
    """A registry sign taken apart: ``IV. ÚS 3523/20`` -> ``("IV", "3523", "20")``.

    ``designator`` is the Roman numeral or the literal ``Pl``; ``year`` is always the
    two-digit form the document key uses.
    """

    designator: str
    sequence: str
    year: str


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


def sign_parts(case_no: str) -> SignParts | None:
    """Take an ÚS case number apart, or None when the string is not one. Pure.

    A four-digit filing year is folded to its last two digits, because that is the form the
    document key uses and the form the court itself prints: the corpus cites the very same
    decision as both ``Pl. ÚS 44/2021`` and ``Pl. ÚS 44/21``, and NALUS answers
    ``Pl-44-21_1`` with a page whose registry sign reads ``Pl.ÚS 44/21``. The fold is
    therefore checked against the page rather than trusted — see
    :func:`jg.crawl.nalus.matches_sign`.
    """
    match = _SIGN_PARTS_RE.match(case_no.replace("\xa0", " ").strip())
    if match is None:
        return None
    plenary, roman, sequence, year = match.groups()
    designator = "Pl" if plenary is not None else roman.upper()
    return SignParts(designator, sequence, year[-2:])


def registry_sign_to_sz(registry_sign: str) -> str:
    """``II.ÚS 2379/08`` -> ``2-2379-08``, the stem of the ``GetText.aspx`` document key.

    Both forms are verified against the live site: the panel form on four case numbers and
    the plenary form on ``Pl. ÚS 44/21`` -> ``Pl-44-21``. Raises ``ValueError`` rather than
    returning something plausible for a string that is not an ÚS registry sign.
    """
    parts = sign_parts(registry_sign)
    if parts is None:
        raise ValueError(f"not a Constitutional Court registry sign: {registry_sign!r}")
    if parts.designator == "Pl":
        panel = "Pl"
    else:
        numeral = _ROMAN.get(parts.designator)
        if numeral is None:
            raise ValueError(
                f"unrecognised ÚS panel numeral {parts.designator!r} in {registry_sign!r}"
            )
        panel = str(numeral)
    return f"{panel}-{parts.sequence}-{parts.year}"


def nalus_sz(case_no: str, ordinal: int = 1) -> str | None:
    """The full NALUS document key for a case number, or None if it is not one.

    ``IV. ÚS 3523/20`` -> ``4-3523-20_1``. ``ordinal`` is the document within the case; a
    key whose ``_1`` does not resolve is data to report, not to paper over, so nothing here
    tries a second ordinal on its own.
    """
    try:
        stem = registry_sign_to_sz(case_no)
    except ValueError:
        return None
    return f"{stem}_{ordinal}"


def text_url(document_key: str) -> str:
    """Full-text URL for a complete document key such as ``4-3523-20_1``."""
    return TEXT_URL_TEMPLATE.format(sz=document_key)


def key_from_url(url: str) -> str | None:
    """The ``sz`` document key carried by a ``GetText.aspx`` URL, or None."""
    values = parse_qs(urlsplit(url).query).get("sz")
    return values[0] if values else None


def namespaced_ecli(document_key: str) -> str:
    """``4-3523-20_1`` -> ``nalus:4-3523-20_1``. See :data:`ECLI_NAMESPACE`."""
    return f"{ECLI_NAMESPACE}:{document_key}"


def alias_spellings(registry_sign: str) -> list[str]:
    """Both spellings of a registry sign seen in the wild, most canonical first.

    NALUS prints ``IV.ÚS 3523/20`` in its header; the decisions that cite it write
    ``IV. ÚS 3523/20``. ``decision_alias`` has to carry both or the 14,711 ÚS mentions in
    the corpus resolve to nothing. Both strings are re-spacings of the sign the page itself
    printed, so no identifier is being invented.
    """
    parts = sign_parts(registry_sign)
    if parts is None:
        return []
    tail = f"ÚS {parts.sequence}/{parts.year}"
    return [f"{parts.designator}. {tail}", f"{parts.designator}.{tail}"]


def matches_sign(case_no: str, registry_sign: str | None) -> bool:
    """True when a cited case number and a fetched page's own sign are the same case.

    The check that makes the two-digit year fold in :func:`sign_parts` safe: the key we
    asked for is only accepted once the page NALUS answered with prints the matching sign.
    """
    if not registry_sign:
        return False
    wanted, got = sign_parts(case_no), sign_parts(registry_sign)
    return wanted is not None and wanted == got


# --------------------------------------------------------------------------- parsing


def _label(tree: HTMLParser, selector: str) -> str | None:
    node = tree.css_first(selector)
    if node is None:
        return None
    value = node.text(deep=True).replace("\xa0", " ").strip()
    return value or None


def _label_lines(tree: HTMLParser, selector: str) -> list[str]:
    """One labelled span split on its ``<br/>``s.

    ``#lblParallelQuotation`` holds two citations for a nález that was also promulgated in
    the Sbírka zákonů (``38/2023 Sb.`` and ``N 11/116 SbNU 87`` on Pl. ÚS 44/21), separated
    only by a break. Reading the span with ``text(deep=True)`` would splice them into one
    unusable string.
    """
    node = tree.css_first(selector)
    if node is None:
        return []
    return [line for line in text_paragraphs(node.html or "") if line]


def _despace(text: str) -> str:
    """Folded and with every space removed, so ``t a k t o :`` reads as ``takto:``."""
    return fold_text(text).replace(" ", "")


def parse_paragraphs(html: str) -> list[str]:
    """The decision body as paragraph strings, in document order. Pure.

    Position is what ``decision_paragraph.idx`` is built from (1-based, see
    :func:`jg.normalize.paragraph_rows`), so nothing here reorders or drops.
    """
    return text_paragraphs(html, selector=CONTENT_SELECTOR)


def is_empty_document(html: str) -> bool:
    """True when the page rendered but carries no decision at all. Pure.

    Both conditions are required — no registry sign *and* no body — so that a page with an
    unfamiliar header layout is still parsed rather than silently discarded as empty.
    """
    tree = HTMLParser(html)
    return not _label(tree, _LABEL_SELECTORS["registry_sign"]) and not parse_paragraphs(html)


def parse_header(html: str) -> NalusHeader:
    """``(case_no, decided_on, journal_no, title)`` off the labelled header spans. Pure.

    The header line reads, on the real IV. ÚS 3523/20 page:
    ``IV.ÚS 3523/20 ze dne 24. 8. 2021  N 144/107 SbNU 211  <headline title>``.

    ``journal_no`` is the **SbNU** citation and nothing else. A nález promulgated in the
    Sbírka zákonů prints its ``38/2023 Sb.`` number in the same span; that is an act number,
    not a reporter citation, and storing it in ``journal_no`` would put an act number in a
    decision column. Most decisions are unpublished and have no SbNU citation at all —
    ``None`` there is the normal state, not a parse failure.
    """
    tree = HTMLParser(html)
    registry_sign = _label(tree, _LABEL_SELECTORS["registry_sign"])
    case_no: str | None = registry_sign
    decided_on: dt.date | None = None
    if registry_sign:
        match = _REGISTRY_SIGN_RE.match(registry_sign)
        if match:
            case_no = match.group("sign").strip()
            decided_on = parse_cs_date(match.group("date"))
    journal_no = next(
        (
            line
            for line in _label_lines(tree, _LABEL_SELECTORS["parallel_citation"])
            if _SBNU_RE.search(line)
        ),
        None,
    )
    title = _label(tree, _LABEL_SELECTORS["popular_name"])
    return NalusHeader(case_no, decided_on, journal_no, title)


def parse_panel_type(html: str) -> PanelType:
    """``PANEL`` or ``PLENARY`` for one page. Pure.

    A ``Pl. ÚS`` registry sign is decisive; otherwise the opening line of the body decides,
    and *senát* is the overwhelming default.
    """
    tree = HTMLParser(html)
    paragraphs = parse_paragraphs(html)
    return panel_type(
        registry_sign=_label(tree, _LABEL_SELECTORS["registry_sign"]),
        heading=" ".join(paragraphs[:2]) if paragraphs else None,
        decision_form=_label(tree, _LABEL_SELECTORS["decision_form"]),
    )


def verdict_from_paragraphs(paragraphs: list[str]) -> str | None:
    """The **výrok** out of an already-parsed paragraph list. Pure. The fallback path.

    Two layouts, both observed on cached pages:

    * the modern one announces the operative part by ending the introduction with the
      literal ``takto:`` (``… jako vedlejší účastnice řízení, takto:``), and
    * the older SbNU typesetting labels it with a bare ``Výrok`` paragraph.

    In both, the operative part runs until ``Odůvodnění``. The search is confined to the
    first few paragraphs: a ``takto:`` deeper in the document is the court quoting the
    decision below, and reading that as this court's own výrok would hand the structural
    ``QUASHED`` rule somebody else's annulment.

    Returns None when neither marker is present, which is a refusal rather than a guess.
    ``RawDecision.verdict_text`` is the only input to the ``QUASHED`` rule (PLAN.md section
    8) and a wrong výrok manufactures a red light, the one failure D8 rules out.
    """
    start: int | None = None
    for index, paragraph in enumerate(paragraphs[:_VERDICT_SEARCH_DEPTH]):
        despaced = _despace(paragraph)
        if despaced in _VERDICT_HEADINGS or despaced.endswith(_VERDICT_OPENERS):
            start = index + 1
    if start is None:
        return None

    tail: list[str] = []
    for paragraph in paragraphs[start:]:
        if _despace(paragraph).startswith(_VERDICT_CLOSERS):
            break
        tail.append(paragraph)
    body = "\n".join(tail).strip()
    return body or None


def bold_verdict(html: str, paragraphs: list[str]) -> str | None:
    """The **výrok** read off the markup rather than off marker words. Pure.

    NALUS sets the operative part, and only the operative part, in ``<b>`` inside
    ``td.DocContent``. That held on every cached page across both layouts and across nález,
    plenary nález and usnesení — including ``I. ÚS 741/06``, which carries neither a
    ``takto:`` nor a ``Výrok`` heading and which the marker path therefore cannot see.

    Two guards keep this from picking up ordinary emphasis deeper in the reasoning:
    the block must start within the first :data:`_VERDICT_SEARCH_DEPTH` paragraphs of the
    document, and a leading ``Výrok`` label is dropped rather than stored as if the court
    had said it.
    """
    content = HTMLParser(html).css_first(CONTENT_SELECTOR)
    if content is None:
        return None
    head = [_despace(p) for p in paragraphs[:_VERDICT_SEARCH_DEPTH]]
    for bold in content.css("b"):
        lines = [line for line in text_paragraphs(bold.html or "") if line]
        while lines and _despace(lines[0]) in _VERDICT_HEADINGS:
            lines.pop(0)
        if not lines or _despace(lines[0]) not in head:
            continue
        body = "\n".join(lines).strip()
        if body:
            return body
    return None


def parse_verdict(html: str) -> str | None:
    """The operative part of one ``GetText.aspx`` page, or None. Pure.

    Markup first (:func:`bold_verdict`), marker words second
    (:func:`verdict_from_paragraphs`). The two agree on every cached page where both fire;
    the markup path additionally covers the SbNU layout that announces the výrok with
    nothing at all.

    Declared as returning an optional string rather than ``str`` on purpose: an empty
    string would be indistinguishable from "this decision annulled nothing", and the
    caller has to be able to tell "no výrok parsed" apart from "a výrok that quashes
    nobody".
    """
    return _verdict(html, parse_paragraphs(html))


def _verdict(html: str, paragraphs: list[str]) -> str | None:
    return bold_verdict(html, paragraphs) or verdict_from_paragraphs(paragraphs)


def parse_gettext(html: str) -> NalusDocument:
    """Parse one ``GetText.aspx`` page into everything it shows. Pure, off-line, no network.

    ``jg.classify.router.crawl_cache_verdicts`` calls this by name on the cached page, which
    is how the výrok reaches the structural classifier without a column in ``decision`` and
    without a second network request.
    """
    tree = HTMLParser(html)
    case_no, decided_on, journal_no, title = parse_header(html)
    paragraphs = parse_paragraphs(html)
    return NalusDocument(
        registry_sign=case_no,
        decided_on=decided_on,
        parallel_citation=journal_no,
        parallel_citations=_label_lines(tree, _LABEL_SELECTORS["parallel_citation"]),
        popular_name=title,
        decision_form=_label(tree, _LABEL_SELECTORS["decision_form"]),
        heading=" ".join(paragraphs[:2]) if paragraphs else None,
        verdict_text=_verdict(html, paragraphs),
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

    ``ecli`` is a required argument, not something this function derives from the decision's
    text: see :data:`ECLI_NAMESPACE`. :func:`parse_decision` supplies it from the document
    key in the URL, which is the only authoritative key NALUS exposes.
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


def parse_decision(html: str, *, source_url: str, fetched_at: dt.datetime) -> RawDecision:
    """One page as a :class:`~jg.models.RawDecision`. Pure: HTML in, rows-to-be out.

    The primary key comes from the ``sz`` parameter of ``source_url`` — the document key
    NALUS itself answers to — rather than from anything read out of the decision's prose.
    A URL with no ``sz`` raises instead of falling back to a key derived from the printed
    sign, because the two could disagree and only one of them is the key.
    """
    document_key = key_from_url(source_url)
    if not document_key:
        raise ValueError(
            f"{source_url!r} carries no NALUS 'sz' document key, so there is no primary key "
            "for this decision; fetch it through fetch_decision()."
        )
    return to_raw_decision(
        parse_gettext(html),
        ecli=namespaced_ecli(document_key),
        source_url=source_url,
        fetched_at=fetched_at,
    )


# --------------------------------------------------------------------------- crawling


def fetch_decision(fetcher: Fetcher, case_no: str, *, ordinal: int = 1) -> RawDecision:
    """Fetch and parse one ÚS decision **by its case number**. Cache-first.

    ``case_no`` is a citation as it appears in another decision, e.g. ``IV. ÚS 3523/20``.
    Three refusals, all reported by ``scripts/load_us_citations.py`` rather than swallowed:
    ``ValueError`` when the string is not an ÚS case number, ``jg.crawl.base.FetchFailed``
    when NALUS answers with an error status, and :class:`EmptyDocument` when it answers 200
    with no decision on the page.

    ``ordinal`` is the document within the case. Nothing here tries a second ordinal on its
    own: a key whose ``_1`` carries nothing is a coverage fact to report, and walking
    ordinals blindly would spend requests guessing at documents nobody has seen.
    """
    document_key = nalus_sz(case_no, ordinal)
    if document_key is None:
        raise ValueError(f"not a Constitutional Court case number: {case_no!r}")
    url = text_url(document_key)
    page = fetcher.fetch(url)
    if is_empty_document(page.text):
        raise EmptyDocument(
            f"{url} returned HTTP {page.status} with an empty document: NALUS has no text "
            f"under document key {document_key!r} for {case_no!r}."
        )
    return parse_decision(page.text, source_url=url, fetched_at=page.fetched_at)


def crawl(
    fetcher: Fetcher,
    limit: int | None = None,
    since: dt.date | None = None,
) -> Iterator[RawDecision]:
    """Refuses: NALUS enumeration is a stateful ASP.NET postback — and is not needed.

    Enumerating the Constitutional Court is not what this project wants. What it wants is
    the ÚS decisions the corpus cites, and those are addressable one by one through
    :func:`fetch_decision`. ``scripts/load_us_citations.py`` reads the case numbers out of
    ``decision_paragraph`` and loads exactly them.
    """
    raise CrawlUnavailable(
        "NALUS enumeration is not implemented: a GET to "
        f"{SEARCH_URL} returns an empty search form because the result set lives in "
        "__VIEWSTATE and server session state. It is also not the way in — run "
        "`make load-us` (scripts/load_us_citations.py), which fetches exactly the ÚS "
        "decisions the crawled corpus cites via fetch_decision(fetcher, case_no)."
    )

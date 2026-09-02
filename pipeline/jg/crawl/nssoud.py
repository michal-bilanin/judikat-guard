"""Nejvyšší správní soud (NSS). PLAN.md milestone M1.

NSS decision texts live at ``https://vyhledavac.nssoud.cz`` — an ASP.NET Core MVC search
over the administrative courts' decision database. That host was added to
``config.ALLOWLISTED_HOSTS`` by hand on 2026-09-02 (CLAUDE.md rule 6); the allowlisted
``www.nssoud.cz`` is a TYPO3 presentation site and publishes no decision text at all, so
without it M1 was unreachable. It serves no ``robots.txt`` (404), which
:class:`~jg.crawl.base.Fetcher` treats as "allow"; the 1 req/s rate limit still applies.

The protocol, all of it observed live:

1. ``GET /`` returns one ``<form>`` with 299 named inputs. Every one of them is collected
   and posted back, exactly as the browser does.
2. The date-range condition is two of those inputs,
   ``…vyhledavaciPodminka[0].vyhledavaciPodminkaHodnota[0].HodnotaDatumACas{Od,Do}``,
   in Czech ``d.m.yyyy`` form, plus ``btSubmit``.
3. ``POST /Home/Index?formular=1&zobrazeniVysledkuVolba=5``. The ``5`` selects the **ECLI
   result view**, and that is the whole reason this module can exist: that view adds an
   ECLI column, so the authoritative ECLI is *read off the page* rather than derived from
   the case number, which CLAUDE.md rule 1 forbids.
4. The result page holds up to 40 rows, the total as ``Počet nalezených záznamů: N``, and
   the infinite-scroll state (``currParams`` / ``currViewId`` / ``currSort``) in an inline
   script. Further pages are ``POST /Home/MyResTRowsCont``, 20 rows each, empty body at the
   end.
5. Full text is ``GET /DokumentOriginal/Html/{id}``. The sibling ``/Text/{id}`` endpoint
   declares ``charset=UTF-16`` and sends UTF-16LE with no BOM; ``base.decode_body`` now
   survives that, but ``/Html/`` is what this module uses.

Two things about that site are worth stating plainly, because they bound what this crawler
can promise:

**Paging is not deterministic.** The server's ``order by`` (``currSort``) ties on almost
every row — most decisions in a window share a date — and each page is a fresh query
execution with a new offset. Two walks of the same 128-record day returned 128 rows each
and only 87 and 83 *distinct* ECLIs. A window that fits in the first page is therefore
exact; a window that needs paging is not. :func:`crawl` answers this by splitting the range
into single days (most days fit in one request) and, for the days that do not, re-walking
up to :data:`MAX_PASSES` times and unioning by ECLI, stopping as soon as the union reaches
the reported total. Each pass is cached under its own ``cache_salt``, so a re-run replays
the identical union with zero requests. When the union still falls short the shortfall is
logged per day and counted in :class:`CrawlReport` — a coverage gap that is stated, per
PLAN.md section 2, rather than hidden.

**The result set is wider than NSS.** The same database serves the regional administrative
courts, so a date window returns rows with ``ECLI:CZ:KSPL:…``, ``ECLI:CZ:MSPH:…`` and so
on. ``decision.court_code`` is a foreign key onto the three courts seeded by
``V3__court_seed.sql``, so only ``ECLI:CZ:NSS:`` rows are yielded. The others are counted
and reported, never silently dropped.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin

from pydantic import BaseModel
from selectolax.parser import HTMLParser

from jg.crawl.base import (
    Fetcher,
    detect_marker,
    fold_text,
    normalise_ws,
    parse_cs_date,
    text_paragraphs,
)
from jg.models import PanelType, RawDecision

log = logging.getLogger(__name__)

COURT_CODE = "NSS"
BASE_URL = "https://vyhledavac.nssoud.cz/"

#: The search form. One GET, then the whole payload goes back.
FORM_URL = BASE_URL

#: ``zobrazeniVysledkuVolba=5`` is the ECLI result view. Do not change it without also
#: changing :data:`ECLI_COLUMN`: the column layout is what the number selects.
SEARCH_URL = BASE_URL + "Home/Index?formular=1&zobrazeniVysledkuVolba=5"

#: Full decision text. ``/Text/`` is UTF-16 without a BOM; ``/Html/`` is honest UTF-8.
DOCUMENT_URL_TEMPLATE = BASE_URL + "DokumentOriginal/Html/{document_id}"

#: Field prefix of the "Datum vydání rozhodnutí" condition, verified against the live form.
DATE_CONDITION = "vyhledavaciSekce[1].vyhledavaciPodminka[0].vyhledavaciPodminkaHodnota[0]"
DATE_FROM_FIELD = DATE_CONDITION + ".HodnotaDatumACasOd"
DATE_TO_FIELD = DATE_CONDITION + ".HodnotaDatumACasDo"
SUBMIT_FIELD = "btSubmit"

#: Sent with every POST, excluded from every cache key: ASP.NET Core mints a fresh
#: antiforgery token per session, so keying on it would mean a re-run never hits its own
#: cache. Verified on 2026-09-02 that ``/Home/Index`` accepts the search without a valid
#: token at all, so replaying a cached form page (whose token is stale) still works.
VOLATILE_FIELDS = ("__RequestVerificationToken",)

#: Result table in the ECLI view, and its column order, verified against the live page:
#: ``# | Datum | Číslo jednací | Soud (senát) | Druh dokumentu | Výrok rozhodnutí NSS |
#: Účastníci řízení | ECLI | Kasační/ústavní stížnost | Možnosti`` — with one unlabelled
#: cell between ``#`` and ``Datum``.
RESULTS_TABLE_SELECTOR = "table#tresults"
ORDINAL_COLUMN = 0
DATE_COLUMN = 2
REF_NO_COLUMN = 3
COURT_PANEL_COLUMN = 4
DOCUMENT_KIND_COLUMN = 5
OUTCOME_COLUMN = 6
PARTIES_COLUMN = 7
ECLI_COLUMN = 8
COLUMN_COUNT = 11

#: How many times a single day may be re-walked when paging loses rows. Four passes took
#: a 128-record day from 87 distinct ECLIs to 126; the cost is paid only on days that
#: actually need paging.
MAX_PASSES = 4

#: Hard stop on the page walk, so a misparsed total cannot spin forever.
MAX_PAGES = 60

#: With no window given, crawl the last month rather than the whole database. Stating a
#: small default is honest; silently starting a multi-hundred-thousand-record fetch is not.
DEFAULT_WINDOW_DAYS = 30

_TOTAL_COUNT_RE = re.compile(r"Počet\s+nalezených\s+záznamů\s*:\s*(\d[\d\s]*)")
_ECLI_RE = re.compile(r"^ECLI:CZ:[A-Z]{2,5}:\d{4}:[A-Za-z0-9._-]+$")
_DOCUMENT_ID_RE = re.compile(r"/DokumentOriginal/(?:Html|Text|Index)/(\d+)")
#: "6 Afs 234/2023 - 50" -> spisová značka plus the sheet number of the č. j.
_REF_NO_RE = re.compile(r"^(?P<case_no>.+?)\s*-\s*(?P<sheet_no>\d+)$")
#: "[12] Nejvyšší správní soud …" — the court's own paragraph numbering.
_PARAGRAPH_NO_RE = re.compile(r"^\[(\d{1,4})\]")

_JS_STRING_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})|\\(.)")


def _js_var(html: str, name: str) -> str | None:
    """The value of ``var <name> = '…';`` from an inline script, unescaped.

    The page writes the paging state as JavaScript string literals with every quote
    encoded as ``\\u0022``. jQuery posts the *unescaped* value, so this has to undo the
    escaping rather than pass the literal through, or the server receives a JSON blob full
    of backslashes and answers with no rows.
    """
    match = re.search(rf"var\s+{re.escape(name)}\s*=\s*'((?:[^'\\]|\\.)*)'\s*;", html)
    if match is None:
        return None
    return _JS_STRING_ESCAPE_RE.sub(
        lambda m: chr(int(m.group(1), 16)) if m.group(1) else m.group(2),
        match.group(1),
    )


# --------------------------------------------------------------------- parsed shapes


class ResultRow(BaseModel):
    """One row of the ECLI result view. Everything here was printed on the page."""

    ecli: str
    decided_on: dt.date
    #: Spisová značka, e.g. "6 Afs 234/2023".
    case_no: str
    #: Č. j. in the form the site's own citation button produces, "6 Afs 234/2023-50".
    ref_no: str
    #: "Soud (senát)" as printed: "tříčlenný senát NSS", "rozšířený senát NSS", or a
    #: regional court's name. The panel signal M3 is built on. PLAN.md section 1.
    court_panel: str
    document_kind: str | None = None
    #: "Výrok rozhodnutí NSS" as a coded value ("zamítnuto", "zrušeno a vráceno").
    #: Not the výrok text: that is read out of the decision itself.
    outcome: str | None = None
    parties: str | None = None
    #: The database key behind /DokumentOriginal/{Index,Text,Html}/{id}.
    document_id: str

    @property
    def document_url(self) -> str:
        return DOCUMENT_URL_TEMPLATE.format(document_id=self.document_id)

    @property
    def is_nss(self) -> bool:
        """True for the Supreme Administrative Court's own decisions.

        Read off the ECLI, which is the authoritative court code on the page. The same
        search returns regional administrative courts, and ``decision.court_code`` has no
        row for those.
        """
        return self.ecli.startswith("ECLI:CZ:NSS:")


@dataclass(frozen=True, slots=True)
class ScrollState:
    """The four inline-script variables the infinite scroll posts back for more rows."""

    more_rows_url: str
    params: str
    view_id: str
    sort: str


class NssDocument(BaseModel):
    """One ``/DokumentOriginal/Html/{id}`` page, before anything is assumed.

    Separate from :class:`jg.models.RawDecision` for the same reason NALUS's document
    shape is: this page carries no ECLI, and it carries a numbering flag for which the
    fixed ``RawDecision`` contract has no column.
    """

    #: The first lines: case number, form of decision, panel composition. Panel detection
    #: reads this and never the body — NSS reasoning quotes the rozšířený senát constantly.
    heading: str | None = None
    #: Case number as printed at the head of the document.
    case_no: str | None = None
    decided_on: dt.date | None = None
    #: The operative part, the passage after "takto:". Only this feeds structural QUASHED.
    verdict_text: str | None = None
    #: 1-based by position; when :attr:`numbered` is true, position N is the court's own
    #: paragraph [N] and the gaps are empty strings.
    paragraphs: list[str] = []
    #: True when the document printed "[1]", "[2]", … and the indices are aligned to it.
    numbered: bool = False


@dataclass
class CrawlReport:
    """What one :func:`crawl` run actually saw. Coverage is a number, not a vibe."""

    days: int = 0
    #: Sum of "Počet nalezených záznamů" over the days walked, all courts.
    reported_total: int = 0
    #: Distinct ECLIs actually seen, all courts.
    rows_seen: int = 0
    nss_rows: int = 0
    other_court_rows: int = 0
    yielded: int = 0
    unnumbered: int = 0
    #: ``{day: (reported, seen)}`` for days where paging lost rows.
    short_days: dict[dt.date, tuple[int, int]] = field(default_factory=dict)

    @property
    def missing(self) -> int:
        return max(self.reported_total - self.rows_seen, 0)


# ------------------------------------------------------------------ panel detection


# Matched on folded (diacritic-free, lowercased) text.
#
# The second pattern is not a guess. The search form's own "Soud (senát)" dial tree — the
# ``ciselnikTreeData`` hidden input, cached under ``data/raw/NSS/`` with the rest of the
# crawl — lists the court's bodies as: kárný senát, kolegium NSS, kompetenční senát NSS,
# plénum NSS, **rozšířený senát NSS** (with children **7členný RS NSS** and **9členný RS
# NSS**), senát NSS pro určení lhůty, tříčlenný senát NSS, volební senát NSS, zvláštní
# senát. The two ``RS`` spellings are the seven- and nine-judge formations of the rozšířený
# senát, and the result column prints them in that abbreviated form. Without the second
# pattern those decisions would come back ``panel``, which is the one direction of error
# that matters here: an extended-panel decision read as an ordinary one silently deletes
# the departure signal M3 is built on. The pattern requires the digit-and-``RS NSS`` shape
# rather than a bare "RS", so it cannot fire on running prose.
_EXTENDED_MARKERS = (
    re.compile(r"\brozsiren\w* senat\w*\b"),
    re.compile(r"\b\d+clenn\w* rs nss\b"),
)

# "plénum NSS", also from that dial tree. The NSS plenum is not the ÚS plenum and
# ``departure_authority`` seeds no row for it, so a treatment from one lands as an amber
# conflict rather than a red supersession — the conservative side, per D8.
_PLENARY_MARKERS = (re.compile(r"\bplen\w* nss\b"),)

# A three-judge panel *referring* a question is still a three-judge panel. Discard the
# marker when one of these governs it. PLAN.md section 1, §17 s. ř. s.
_REFERRAL_VERBS = re.compile(
    r"\b(?:postoup\w*|postupuje|postupuji|predlozi\w*|predlozen\w*|predklad\w*|"
    r"prikaz\w*|navrh\w* na postoupeni)\b"
)


def panel_type(
    *,
    court_panel: str | None = None,
    heading: str | None = None,
    case_no: str | None = None,
    decision_kind: str | None = None,
) -> PanelType:
    """Classify the panel that issued an NSS decision. Pure; safe to call on short strings.

    ``court_panel`` is the result page's "Soud (senát)" column, which is the cheapest and
    most reliable signal there is: it says ``tříčlenný senát NSS`` for an ordinary panel
    and names the ``rozšířený senát`` for an extended one. ``heading`` is the first lines
    of the decision itself ("Nejvyšší správní soud rozhodl v rozšířeném senátu složeném
    z …"), used when no metadata is available.

    Feed it metadata or the heading, never the body: a body-wide match would label most of
    the corpus ``extended`` and manufacture authority that PLAN.md section 9 turns straight
    into red lights.

    ``EXTENDED`` is asserted only on a marker that survives the referral-verb guard, so a
    panel *handing a question over* is never mistaken for the panel that answers it.
    Returns ``UNKNOWN`` only when there is genuinely nothing to read, because ``unknown``
    is a coverage signal rather than a default.
    """
    fields = [f for f in (court_panel, heading, case_no, decision_kind) if f and f.strip()]
    if not fields:
        return PanelType.UNKNOWN
    haystack = " \n ".join(fields)
    if detect_marker(haystack, _EXTENDED_MARKERS, referral_verbs=_REFERRAL_VERBS):
        return PanelType.EXTENDED
    if detect_marker(haystack, _PLENARY_MARKERS, referral_verbs=_REFERRAL_VERBS):
        return PanelType.PLENARY
    return PanelType.PANEL


# ------------------------------------------------------------------- search payload


def parse_search_form(html: str) -> dict[str, str]:
    """Every named input of the search form, as the browser would submit it.

    All 299 of them, hidden fields and the antiforgery token included. Radios and
    checkboxes contribute only when they are checked, which is what makes the posted
    payload mean "the form as it was rendered" rather than "every option at once".
    """
    tree = HTMLParser(html)
    form = tree.css_first("form")
    if form is None:
        raise ValueError("no <form> on the NSS search page; the site layout changed")
    fields: dict[str, str] = {}
    for node in form.css("input, select, textarea"):
        attrs = node.attributes
        name = attrs.get("name")
        if not name:
            continue
        kind = (attrs.get("type") or node.tag).lower()
        if kind in ("radio", "checkbox") and attrs.get("checked") is None:
            continue
        if node.tag == "select":
            selected = [o for o in node.css("option") if o.attributes.get("selected") is not None]
            option = selected[0] if selected else node.css_first("option")
            fields[name] = (option.attributes.get("value") or "") if option else ""
        elif node.tag == "textarea":
            fields[name] = node.text(deep=True) or ""
        else:
            fields[name] = attrs.get("value") or ""
    if DATE_FROM_FIELD not in fields:
        raise ValueError(
            f"the search form no longer carries {DATE_FROM_FIELD!r}; the date-range "
            "condition moved and the payload below would silently search everything"
        )
    return fields


def cs_date(day: dt.date) -> str:
    """``1.1.2024``. The format the form's date inputs take."""
    return f"{day.day}.{day.month}.{day.year}"


def search_payload(
    form_fields: Mapping[str, str], since: dt.date, until: dt.date
) -> dict[str, str]:
    """The form payload with the date-range condition filled in. Pure."""
    payload = dict(form_fields)
    payload[DATE_FROM_FIELD] = cs_date(since)
    payload[DATE_TO_FIELD] = cs_date(until)
    payload[SUBMIT_FIELD] = ""
    return payload


def page_payload(state: ScrollState, page_num: int) -> dict[str, str]:
    """The payload ``/Home/MyResTRowsCont`` takes for page ``page_num``."""
    return {
        "vyhledavaciPodminky": state.params,
        "zobrazeniVysledkuId": state.view_id,
        "pageNum": str(page_num),
        "resultOrder": state.sort,
    }


# ------------------------------------------------------------------- result parsing


def parse_total_count(html: str) -> int:
    """``Počet nalezených záznamů: 912`` -> ``912``.

    The paging bound and the sanity check in one: a walk that ends with fewer distinct
    ECLIs than this says so. Raises ``ValueError`` when the phrase is absent, because a
    silently assumed zero would look exactly like an empty day.
    """
    match = _TOTAL_COUNT_RE.search(html.replace("\xa0", " ").replace("&#xA0;", " "))
    if match is None:
        raise ValueError("no 'Počet nalezených záznamů' on the page; the search failed")
    return int(re.sub(r"\s", "", match.group(1)))


def parse_scroll_state(html: str) -> ScrollState | None:
    """``(more_rows_url, params, view_id, sort)`` from the inline script, or ``None``.

    ``None`` means the page carries no paging state, which is the normal shape of a result
    set that fits on one page.
    """
    names = ("moreRowsUrl", "currParams", "currViewId", "currSort")
    values = {name: _js_var(html, name) for name in names}
    if any(value is None for value in values.values()):
        return None
    return ScrollState(
        more_rows_url=values["moreRowsUrl"] or "",
        params=values["currParams"] or "",
        view_id=values["currViewId"] or "",
        sort=values["currSort"] or "",
    )


def _cell(cells: Sequence[object], index: int) -> str:
    node = cells[index]
    return normalise_ws(node.text(deep=True))  # type: ignore[attr-defined]


def parse_result_rows(html: str) -> list[ResultRow]:
    """Rows of the ECLI result view. Takes a whole result page *or* a paging fragment.

    A fragment is a bare ``<tbody>`` of ``<tr>``; an HTML parser drops those unless they
    sit inside a table, hence the wrap. Rows whose ECLI does not have the published shape,
    or which carry no document link, are skipped and logged: no identifier here is ever
    reconstructed, per CLAUDE.md rule 1.
    """
    tree = HTMLParser(html)
    table = tree.css_first(RESULTS_TABLE_SELECTOR)
    if table is None:
        table = HTMLParser(f"<table>{html}</table>").css_first("table")
    if table is None:
        return []

    rows: list[ResultRow] = []
    for tr in table.css("tr"):
        cells = tr.css("td")
        if len(cells) < COLUMN_COUNT:
            continue
        ecli = _cell(cells, ECLI_COLUMN)
        if not _ECLI_RE.match(ecli):
            log.debug("skipping a result row with an unrecognised ECLI %r", ecli)
            continue
        links = " ".join(
            a.attributes.get("href") or "" for a in tr.css("a") if a.attributes.get("href")
        )
        id_match = _DOCUMENT_ID_RE.search(links)
        if id_match is None:
            log.warning("no /DokumentOriginal/ link on the row for %s; skipping", ecli)
            continue
        try:
            decided_on = parse_cs_date(_cell(cells, DATE_COLUMN))
        except ValueError:
            log.warning("unparseable date on the row for %s; skipping", ecli)
            continue
        printed_ref_no = _cell(cells, REF_NO_COLUMN)
        case_no, ref_no = split_ref_no(printed_ref_no)
        rows.append(
            ResultRow(
                ecli=ecli,
                decided_on=decided_on,
                case_no=case_no,
                ref_no=ref_no,
                court_panel=_cell(cells, COURT_PANEL_COLUMN),
                document_kind=_cell(cells, DOCUMENT_KIND_COLUMN) or None,
                outcome=_cell(cells, OUTCOME_COLUMN) or None,
                parties=_cell(cells, PARTIES_COLUMN) or None,
                document_id=id_match.group(1),
            )
        )
    return rows


def split_ref_no(printed: str) -> tuple[str, str]:
    """``6 Afs 234/2023 - 50`` -> ``("6 Afs 234/2023", "6 Afs 234/2023-50")``.

    The two are different aliases of the same decision and both must be stored: a document
    may cite either the spisová značka or the full č. j. The dash is closed up because that
    is the form the site's own "Citace" button produces (*čj. 9 As 77/2022-27*) **and** the
    form :func:`jg.extract.resolver.alias_candidates` builds from the ``REF_NO`` pattern in
    ``extract/patterns.toml``. Getting that spacing wrong would store an alias no citation
    could ever match, which is the entire point of the table.

    A value with no sheet number comes back unchanged in both positions.
    """
    value = normalise_ws(printed)
    match = _REF_NO_RE.match(value)
    if match is None:
        return value, value
    case_no = normalise_ws(match.group("case_no"))
    return case_no, f"{case_no}-{match.group('sheet_no')}"


# ----------------------------------------------------------------- decision parsing


def align_paragraphs(paragraphs: Sequence[str]) -> tuple[list[str], bool]:
    """Line the paragraph list up with the court's own ``[N]`` numbering.

    ``citation.paragraph_idx`` is only worth storing if a lawyer can look the number up in
    the published decision, so when the document numbers its paragraphs the returned list
    is positioned such that index ``N-1`` holds paragraph ``[N]``. Positions with no
    numbered paragraph become empty strings, which ``jg.normalize.paragraph_rows`` drops
    without renumbering anything after them.

    The cost is stated rather than hidden: the unnumbered material — the case heading, the
    section titles, the výrok, the Poučení and the signature block — is not stored as a
    paragraph. The výrok is kept separately in ``RawDecision.verdict_text``, which is the
    only part of it any rule reads.

    Returns ``(paragraphs, True)`` when numbering was found, and the input unchanged with
    ``False`` when it was not, in which case the index is a plain sequential position.
    """
    numbered: dict[int, str] = {}
    for body in paragraphs:
        match = _PARAGRAPH_NO_RE.match(body)
        if match is None:
            continue
        number = int(match.group(1))
        if number >= 1:
            numbered.setdefault(number, body)
    # One stray "[1]" in a quotation is not a numbering scheme. Require a real run.
    if len(numbered) < 3 or 1 not in numbered:
        return list(paragraphs), False
    highest = max(numbered)
    aligned = [numbered.get(index, "") for index in range(1, highest + 1)]
    return aligned, True


def _verdict_text(paragraphs: Sequence[str]) -> str | None:
    """The výrok: the passage introduced by "takto:", up to the reasoning.

    Only this feeds the structural QUASHED rule (PLAN.md section 8), so it is taken
    verbatim and nothing else is folded into it.
    """
    for index, paragraph in enumerate(paragraphs):
        if not fold_text(paragraph).startswith("takto"):
            continue
        tail: list[str] = []
        for following in paragraphs[index + 1 :]:
            folded = fold_text(following)
            if folded.startswith("oduvodneni") or _PARAGRAPH_NO_RE.match(following):
                break
            tail.append(following)
        body = "\n".join(tail).strip()
        return body or None
    return None


def parse_document(html: str) -> NssDocument:
    """Parse one ``/DokumentOriginal/Html/{id}`` page. Pure, off-line, no network."""
    raw_paragraphs = text_paragraphs(html)
    heading = " ".join(raw_paragraphs[:4]) if raw_paragraphs else None
    case_no = raw_paragraphs[0] if raw_paragraphs else None
    decided_on: dt.date | None = None
    # "V Brně dne 4. ledna 2024" sits at the foot of every decision. Read it from the end,
    # where the only date is the decision's own; the body is full of other dates.
    for paragraph in reversed(raw_paragraphs):
        try:
            decided_on = parse_cs_date(paragraph)
        except ValueError:
            continue
        break
    paragraphs, numbered = align_paragraphs(raw_paragraphs)
    return NssDocument(
        heading=heading,
        case_no=case_no,
        decided_on=decided_on,
        verdict_text=_verdict_text(raw_paragraphs),
        paragraphs=paragraphs,
        numbered=numbered,
    )


#: ``jg.classify.router.crawl_cache_verdicts`` looks for a ``parse_gettext`` on the court
#: module and calls it with a cached page's text to recover the výrok. The name is that
#: contract (it comes from NALUS's ``GetText.aspx``), not a description of this endpoint.
parse_gettext = parse_document


def parse_decision(
    html: str,
    *,
    row: ResultRow,
    source_url: str,
    fetched_at: dt.datetime,
) -> RawDecision:
    """Lift one decision page into the crawl -> normalize seam.

    ``row`` is required, not optional. The ECLI, the date and the panel are read from the
    ECLI result view; the decision page prints none of them in a machine-readable form, and
    deriving an ECLI from a case number is exactly what CLAUDE.md rule 1 forbids. The
    document supplies the text, the výrok and nothing else that is trusted over the row.
    """
    return to_raw_decision(
        parse_document(html), row=row, source_url=source_url, fetched_at=fetched_at
    )


def to_raw_decision(
    document: NssDocument,
    *,
    row: ResultRow,
    source_url: str,
    fetched_at: dt.datetime,
) -> RawDecision:
    """The already-parsed-document half of :func:`parse_decision`.

    Exists so the crawl can read ``NssDocument.numbered`` without parsing the page twice.

    ``journal_no`` stays ``None``: neither the result view nor the decision page shows an
    R-číslo, and inventing one is not an option.
    """
    return RawDecision(
        ecli=row.ecli,
        court_code=COURT_CODE,
        panel_type=panel_type(court_panel=row.court_panel, heading=document.heading),
        decided_on=row.decided_on,
        case_no=row.case_no,
        ref_no=row.ref_no,
        journal_no=None,
        ratio_summary=None,
        source_url=source_url,
        fetched_at=fetched_at,
        paragraphs=document.paragraphs,
        verdict_text=document.verdict_text,
    )


# ----------------------------------------------------------------------- the crawl


def _days(since: dt.date, until: dt.date) -> Iterator[dt.date]:
    day = since
    while day <= until:
        yield day
        day += dt.timedelta(days=1)


def collect_day(
    fetcher: Fetcher,
    form_fields: Mapping[str, str],
    day: dt.date,
    *,
    passes: int = MAX_PASSES,
) -> tuple[int, dict[str, ResultRow]]:
    """Every result row for one day, as ``(reported_total, {ecli: row})``.

    One pass is one search plus its page walk. A day that fits on the first page needs
    exactly one request and is exact. A day that needs paging is re-walked, each pass under
    its own cache salt so it is a genuinely new sample rather than a replay, until the union
    reaches the reported total or ``passes`` is exhausted. The shortfall, if any, is the
    caller's to report.
    """
    payload = search_payload(form_fields, day, day)
    found: dict[str, ResultRow] = {}
    total = 0

    for attempt in range(passes):
        salt = "" if attempt == 0 else f"pass{attempt}"
        page = fetcher.fetch_post(
            SEARCH_URL, payload, cache_omit=VOLATILE_FIELDS, cache_salt=salt
        )
        total = parse_total_count(page.text)
        if total == 0:
            return 0, {}
        seen_this_pass = 0
        for row in parse_result_rows(page.text):
            found.setdefault(row.ecli, row)
            seen_this_pass += 1
        state = parse_scroll_state(page.text)

        page_num = 1
        while state is not None and seen_this_pass < total and page_num <= MAX_PAGES:
            fragment = fetcher.fetch_post(
                urljoin(BASE_URL, state.more_rows_url),
                page_payload(state, page_num),
                cache_salt=salt,
            )
            rows = parse_result_rows(fragment.text)
            if not rows:
                break
            for row in rows:
                found.setdefault(row.ecli, row)
                seen_this_pass += 1
            page_num += 1

        if len(found) >= total:
            break
        if attempt + 1 < passes:
            log.info(
                "%s: pass %d saw %d of %d records (the site's paging is not stable); "
                "taking another sample",
                day.isoformat(),
                attempt + 1,
                len(found),
                total,
            )

    return total, found


def crawl(
    fetcher: Fetcher,
    limit: int | None = None,
    since: dt.date | None = None,
    until: dt.date | None = None,
    *,
    passes: int = MAX_PASSES,
    report: CrawlReport | None = None,
) -> Iterator[RawDecision]:
    """NSS decisions decided in ``[since, until]``, one :class:`RawDecision` at a time.

    Day by day, oldest first, so an interrupted run resumes from its cache and only pays
    for the days it has not reached. Every request — the form, each search, each paging
    fragment, each decision — goes through the cache, so a second run over the same window
    issues zero network requests.

    ``limit`` stops the iterator after that many decisions; it bounds the *yield*, not the
    listing, so the day it stops in is fully listed and cached and the next run continues
    from disk. Pass ``report`` to receive the coverage counters the run accumulates.
    """
    until = until or dt.date.today()
    if since is None:
        since = until - dt.timedelta(days=DEFAULT_WINDOW_DAYS)
        log.warning(
            "no date window given; crawling %s..%s. The bulk load passes since= and until= "
            "explicitly — this default deliberately does not reach for the whole database.",
            since.isoformat(),
            until.isoformat(),
        )
    if since > until:
        raise ValueError(
            f"empty window: since={since.isoformat()} is after until={until.isoformat()}"
        )

    tally = report if report is not None else CrawlReport()
    form_fields = parse_search_form(fetcher.get(FORM_URL))
    log.info("NSS: %d form fields, crawling %s..%s", len(form_fields), since, until)

    for day in _days(since, until):
        total, rows = collect_day(fetcher, form_fields, day, passes=passes)
        tally.days += 1
        tally.reported_total += total
        tally.rows_seen += len(rows)
        if len(rows) < total:
            tally.short_days[day] = (total, len(rows))
            log.warning(
                "%s: %d of %d records recovered after %d passes; the rest are a stated "
                "coverage gap, not a silent one",
                day.isoformat(),
                len(rows),
                total,
                passes,
            )

        for row in sorted(rows.values(), key=lambda r: r.ecli):
            if not row.is_nss:
                tally.other_court_rows += 1
                continue
            tally.nss_rows += 1
            page = fetcher.fetch(row.document_url)
            document = parse_document(page.text)
            decision = to_raw_decision(
                document,
                row=row,
                source_url=row.document_url,
                fetched_at=page.fetched_at,
            )
            if not decision.paragraphs:
                log.warning("%s: no paragraphs parsed from %s", row.ecli, row.document_url)
            elif not document.numbered:
                tally.unnumbered += 1
                log.info(
                    "%s prints no [N] paragraph numbering; paragraph_idx for it is a "
                    "sequential position, not a number a reader can look up",
                    row.ecli,
                )
            tally.yielded += 1
            yield decision
            if limit is not None and tally.yielded >= limit:
                log.info("NSS: stopping at limit=%d", limit)
                return

    log.info(
        "NSS: %d days, %d of %d records listed, %d NSS (%d other courts), %d yielded, "
        "%d without printed paragraph numbers",
        tally.days,
        tally.rows_seen,
        tally.reported_total,
        tally.nss_rows,
        tally.other_court_rows,
        tally.yielded,
        tally.unnumbered,
    )

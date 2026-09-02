"""e-Sbírka statute ingest: the live API, dump reading, timeline construction.

PLAN.md section 10, milestone M6. This is the input side of the provision-rewording
differentiator: a decision from 2013 interpreting a rule reworded in 2017 has a spotless
citation history and is worthless, and the only thing that finds it is a *version
timeline* — for one provision, which wording was in force on which day.

The live API, and why M6 is now possible
---------------------------------------

``https://e-sbirka.gov.cz/sbr-externi`` is public: no key, no datová schránka. PLAN.md
section 10's parenthetical (bulk dumps only, the REST API needs registration) is out of
date and :data:`API_BASE_URL` is the replacement. The host was added to
:data:`jg.config.ALLOWLISTED_HOSTS` on explicit user authorisation on 2026-09-03.

The mechanism the whole milestone hangs on is that a *staleUrl* is an ELI path which
accepts a **date**, and resolves to the wording in force on that date::

    /eli/cz/sb/2012/89               the wording in force now
    /eli/cz/sb/2012/89/2014-01-01    the wording in force on 1 January 2014

That is section 10's question stated as a URL. The routes used here, all verified live
against act 89/2012 and all cached under ``data/raw/ESBIRKA/``:

``/dokumenty-sbirky/{staleUrl}/id``
    plain integer body — the document id of that wording.
``/dokumenty-sbirky/{dokumentId}/detail-zneni``
    ``citace`` (``"89/2012 Sb."``), ``nazev``, and the wording's
    ``datumUcinnostiZneniOd`` / ``...Do``. Used to confirm the act number rather than
    trust the one that was asked for.
``/dokumenty-sbirky/{staleUrl}/historie``
    every wording of the act with its effective-from and effective-to dates, its
    ``typZneni``, and the amending acts. ``datumUcinnostiZneniDo`` is **inclusive** (a
    window ends 2025-06-30 and the next opens 2025-07-01), which is exactly the convention
    ``ProvisionVersionRecord.valid_to`` and ``ProvisionRepository.VERSION_IN_FORCE`` use.
``/dokumenty-sbirky/{staleUrl}/fragmenty?cisloStranky=N``
    the wording's provisions, one *fragment* per §, per odstavec, per písmeno, with
    ``eli`` (the structural path, ``.../par_1180/odst_1``), ``zkracenaCitace`` and
    ``xhtml`` (the wording itself). **``cisloStranky`` is 0-based**: pages run
    ``0 .. pocetStranek - 1``, and page 1 silently starts a third of the way into the act.

``/obsah`` (a lazy table-of-contents tree) is not used: it truncates ``textUstanoveni`` at
about 250 characters, and a body cut mid-sentence would make two identical wordings
compare equal or two different ones compare unequal at random.

Retired predecessor, kept because the refusal has to stay checkable
------------------------------------------------------------------

``https://opendata.eselpoint.cz/`` is a **catch-all**: every path returns HTTP 200 with the
same 41 341-byte notice that the service moved. Checked against ``/robots.txt``,
``/sitemap.xml``, ``/esel-esb/``, ``/esb-otevrena-data/``, ``/opendata/``, ``/data/``,
``/eli/``, ``/api/`` and a deliberately nonsense path, all byte-identical; the nonsense
path is what proves it is a catch-all rather than eight real endpoints.
:func:`probe_open_data` re-runs that check on demand, cache-first, and
:func:`is_moved_notice` is written against the markup actually on disk.

:func:`parse_dump` stays a loud :class:`~jg.crawl.base.ParserSeam`. Nobody has seen
e-Sbírka's bulk *dump* schema — the live API above is a different thing — and a guessed key
path would put wrong statute text or a wrong ``valid_from`` into the database, which turns
a correct GREEN into a confidently wrong AMBER with a full evidence trail behind it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel
from selectolax.parser import HTMLParser

from jg import config
from jg.crawl.base import CrawlUnavailable, Fetcher, FetchError, ParserSeam, normalise_ws

log = logging.getLogger(__name__)

__all__ = [
    "API_BASE_URL",
    "BASE_URL",
    "DUMP_SUFFIXES",
    "EsbirkaApiError",
    "Fragment",
    "LOCAL_FORMAT",
    "MAX_DUMP_BYTES",
    "OpenDataProbe",
    "ProvisionRecord",
    "ProvisionVersionRecord",
    "SOURCE_CODE",
    "SUCCESSOR_URL",
    "TimelineError",
    "Wording",
    "act_eli",
    "api_url",
    "build_timeline",
    "crawl",
    "fetch_api",
    "fetch_fragments",
    "fetch_history",
    "fetch_provision_versions",
    "fetch_wording_detail",
    "fragment_keys",
    "fragment_text",
    "group_by_provision",
    "is_moved_notice",
    "iter_dump_files",
    "iter_dump_records",
    "normalise_section",
    "parse_dump",
    "parse_history",
    "parse_local_dump",
    "probe_open_data",
    "resolve_document_id",
    "versions_from_fragments",
    "wording_in_force",
    "wording_stale_url",
]

#: Not a court. ``court.code`` never takes this value; it is a cache namespace under
#: ``data/raw/`` and a label in log output. Named ``SOURCE_CODE`` rather than
#: ``COURT_CODE`` for exactly that reason, and ``jg.crawl.SOURCES`` special-cases it.
SOURCE_CODE = "ESBIRKA"
BASE_URL = "https://opendata.eselpoint.cz/"

#: The live public API. Taken from the SPA's own configuration at
#: ``https://e-sbirka.gov.cz/assets/configs/env.js``, where ``dasexApiBasePath`` is
#: ``https://e-sbirka.{{domain}}/sbr-externi`` and ``{{domain}}`` is ``gov.cz``. No key,
#: no authentication. Errors come back as ``{"chyby": [{"kod": ..., "popis": ...}]}``.
API_BASE_URL = "https://e-sbirka.gov.cz/sbr-externi"

#: ELI namespace for the Sbírka zákonů. ``/eli/cz/sb/{year}/{number}`` identifies an act;
#: appending ``/{date}`` selects the wording in force on that date. Anything longer — a
#: structural path such as ``.../dokument/norma/cast_1`` — is rejected by the API with
#: *"Nejedná se o staleUrl"*, so nothing here ever builds one.
ELI_PREFIX = "/eli/cz/sb"

#: Where the service moved. Present so the escalation message can name it. Deliberately
#: never fetched: not in :data:`jg.config.ALLOWLISTED_HOSTS`, and CLAUDE.md rule 6 says
#: adding a host is a decision to escalate, not a code change to make.
SUCCESSOR_URL = "https://e-sbirka.gov.cz"

#: Paths :func:`probe_open_data` checks. The last one is nonsense on purpose: if it comes
#: back identical to the others, the host is a catch-all and no amount of path-guessing
#: will find a dump. Without that control the other eight 200s would look like endpoints.
PROBE_PATHS: tuple[str, ...] = (
    "robots.txt",
    "sitemap.xml",
    "esel-esb/",
    "esb-otevrena-data/",
    "opendata/",
    "data/",
    "eli/",
    "api/",
    "nonexistent-probe-xyz",
)

#: Substrings of the notice page as cached in ``data/raw/ESBIRKA/``. Plain substrings, not
#: a regex: the page is HTML-entity encoded in places (``Sb&iacute;rka``) and the only
#: parts that can be matched reliably are the bare-ASCII URLs and the un-encoded words.
MOVED_NOTICE_MARKERS: tuple[str, ...] = (
    "e-sbirka.gov.cz",
    "e-legislativa.gov.cz",
)

#: Extensions treated as dumps. ``.jsonl`` / ``.ndjson`` are read a line at a time and are
#: therefore the only format that survives a genuinely large dump; see
#: :func:`iter_dump_records`.
DUMP_SUFFIXES = (".json", ".jsonld", ".jsonl", ".ndjson")

#: Refuse to slurp a whole-file JSON dump bigger than this. ``json.load`` has no streaming
#: mode, so a 2 GB ``.json`` is an OOM, not a slow read. The message says how to proceed
#: (convert to JSON Lines) rather than just failing.
MAX_DUMP_BYTES = 256 * 1024 * 1024

#: The repo's own interchange format. **Not** an e-Sbírka format and not presented as one:
#: it is what an operator writes by hand or generates from whatever source a human has
#: approved, so that the ingest below has something real to consume. One JSON object per
#: provision version, flat or with the ``provision`` sub-object that
#: ``ProvisionVersionRecord.model_dump()`` emits, so a dump round-trips.
LOCAL_FORMAT = "jg-provisions.v1"


# ------------------------------------------------------------------- parsed shapes


class ProvisionRecord(BaseModel):
    """One row for ``provision``. Columns exactly as in ``V1__init.sql``.

    The tuple ``(act_no, section, subsec)`` is also the key
    ``jg.extract.resolver.provision_key`` produces and the one
    ``ProvisionRepository.ProvisionKey`` resolves against, so the three must agree
    character for character or an ingested provision will never match a citation.
    """

    #: '89/2012' — number/year as printed in the Sbírka, never reformatted.
    act_no: str
    #: '2000' — the paragraph number, with its letter suffix if it has one, no '§'.
    section: str
    #: '1' for *odst. 1*. ``None`` means the citation named no subsection, which is a
    #: different provision row from one that did.
    subsec: str | None = None

    def key(self) -> tuple[str, str, str | None]:
        return self.act_no, self.section, self.subsec

    def label(self) -> str:
        """``§ 2000 odst. 1 zákona č. 89/2012 Sb.`` — for logs and prompts."""
        subsec = f" odst. {self.subsec}" if self.subsec else ""
        return f"§ {self.section}{subsec} zákona č. {self.act_no} Sb."


class ProvisionVersionRecord(BaseModel):
    """One row for ``provision_version``. Columns exactly as in ``V1__init.sql``.

    ``valid_to`` is **inclusive**: the last day this wording was in force. That is not a
    free choice — ``ProvisionRepository.VERSION_IN_FORCE`` asks
    ``valid_to is null or valid_to >= :date``, so a half-open end date would report the
    wrong wording on exactly one day per amendment, silently.

    ``valid_to`` is ``None`` for the wording currently in force. ``derogated_by`` carries
    an ÚS ECLI only when the Constitutional Court struck the provision down (*derogace*);
    it is normally filled by the decision side, not by a statute dump.
    """

    provision: ProvisionRecord
    body: str
    valid_from: dt.date
    valid_to: dt.date | None = None
    derogated_by: str | None = None


@dataclass(frozen=True, slots=True)
class OpenDataProbe:
    """What :func:`probe_open_data` observed. Evidence for the refusal, not a guess."""

    #: path -> (status, body length, sha-free identity marker) for each probed path.
    responses: dict[str, tuple[int, int]]
    #: True when every probed path, including the nonsense control, returned the same body.
    catch_all: bool
    #: True when those bodies are the "we moved" notice.
    moved_notice: bool
    #: True when nothing had to go over the network (CLAUDE.md rule 5 on a re-run).
    fully_cached: bool

    def summary(self) -> str:
        lines = [f"{BASE_URL} probe ({'all cached' if self.fully_cached else 'network used'}):"]
        for path, (status, length) in self.responses.items():
            lines.append(f"  {status} {length:>8} B  /{path}")
        lines.append(f"  catch-all: {self.catch_all}   moved-notice: {self.moved_notice}")
        return "\n".join(lines)


# ------------------------------------------------------------------ observed parsing


def is_moved_notice(html: str) -> bool:
    """Is this the "e-Sbírka has moved" notice page?

    Written against the body actually cached in ``data/raw/ESBIRKA/``. The check is
    deliberately narrow — both successor hostnames must appear — so that the day the host
    starts serving something else, :func:`probe_open_data` reports ``moved_notice=False``
    and the operator is told to map a real dump rather than being handed a stale refusal.
    """
    return all(marker in html for marker in MOVED_NOTICE_MARKERS)


def normalise_section(text: str) -> str:
    """``'§ 2000'`` / ``'2000.'`` -> ``'2000'``.

    Pure string work, no regex. A section number is stored as it is *cited*, because
    ``provision`` is keyed on it and ``jg.extract.patterns`` captures the bare number: a
    stored ``'§ 2000'`` would simply never resolve.
    """
    cleaned = text.replace("§", " ").strip()
    return cleaned.rstrip(".").strip()


def _normalise_subsec(value: object) -> str | None:
    """``'odst. 1'`` / ``'(1)'`` / ``1`` -> ``'1'``; blank or missing -> ``None``."""
    if value is None:
        return None
    text = str(value).replace("odst.", " ").strip().strip("()").strip()
    return text or None


# ------------------------------------------------------------------------ the live API


class EsbirkaApiError(FetchError):
    """The e-Sbírka API answered with something this module will not guess at.

    A :class:`~jg.crawl.base.FetchError` subclass so that a caller which already handles
    fetch failures handles this too. The API's own error shape is
    ``{"chyby": [{"kod": "NEPLATNE_STALE_URL", "popis": "..."}]}``; the body of a 4xx does
    not reach here (``Fetcher`` raises on the status), so the message says which URL was
    asked for and leaves the diagnosis to the operator rather than inventing one.
    """


#: ``'89/2012'`` — number/year exactly as the Sbírka prints it, which is also the format
#: ``provision.act_no`` stores and ``jg.extract`` captures. Anything else is refused rather
#: than coerced: a mis-parsed act number silently addresses a different statute.
_ACT_NO_RE = re.compile(r"^(\d+)/(\d{4})$")

#: A section number as it may legally appear: digits with an optional letter suffix
#: (``2000``, ``1180``, ``2000a``). Used to validate what came *out of* an ELI path, never
#: to construct one.
_SECTION_RE = re.compile(r"^\d+[a-z]?$")

#: Structural segments of a fragment ELI. ``.../par_1180/odst_1/pism_b`` is § 1180 odst. 1
#: písm. b); ``.../par_1176/frag_7984379`` is the single unnumbered paragraph of § 1176.
#: ``provision`` has no column below ``subsec``, so písmena and body fold into the
#: odstavec they belong to, which is also how a citation like *§ 1170 odst. 2* resolves.
_PAR_SEGMENT_RE = re.compile(r"/par_([^/]+)")
_ODST_SEGMENT_RE = re.compile(r"/par_[^/]+/odst_([^/]+)")

#: ``typZneni`` values that are **not** points on a validity timeline. Both were observed
#: live on act 89/2012 and both are traps:
#:
#: ``VYHLASENE``
#:     the act as promulgated in the Sbírka. It carries the publication date as its start
#:     and **no end date at all**, so leaving it in overlaps every consolidated wording.
#:     ``build_timeline`` would then clamp it rather than reject it — it looks exactly like
#:     a legitimate open window — and the 2012 text would silently become the wording in
#:     force for the next eight years.
#: ``MINULE_NEUCINNE``
#:     the same promulgated text, dated from publication to the day before the act took
#:     effect. Storing it asserts a court could have applied it, which is false: 89/2012
#:     was published 2012-03-22 and took effect 2014-01-01.
#:
#: A denylist rather than an allowlist, so that a ``typZneni`` nobody has seen yet is kept
#: and shows up, instead of being dropped into silence. The open-window guard in
#: :func:`fetch_provision_versions` is the backstop for that case.
NON_TIMELINE_WORDING_KINDS = frozenset({"VYHLASENE", "MINULE_NEUCINNE"})


class Wording(BaseModel):
    """One *znění* of an act: the whole statute as it read over one validity window.

    Straight off ``/historie``. ``valid_to`` is inclusive and ``None`` on the current
    wording, matching :class:`ProvisionVersionRecord` and the Java lateral join.
    """

    act_no: str
    valid_from: dt.date
    valid_to: dt.date | None = None
    #: ``AKTUALNI`` | ``MINULE`` | ``MINULE_NEUCINNE`` | ... — passed through verbatim.
    kind: str
    #: ``/sb/2012/89/2014-01-01`` as the API returns it. Recorded for traceability; the
    #: request path is rebuilt from ``act_no`` and ``valid_from`` so it is always the
    #: full ``/eli/cz/sb/...`` form the API documents.
    stale_url: str | None = None
    #: ``('163/2020 Sb.', ...)`` — the amending acts that produced this wording.
    amendments: tuple[str, ...] = ()

    @property
    def on_timeline(self) -> bool:
        """Does this wording belong on a "what was in force when" timeline at all?"""
        return self.kind.upper() not in NON_TIMELINE_WORDING_KINDS

    def eli(self) -> str:
        return wording_stale_url(self.act_no, self.valid_from)


class Fragment(BaseModel):
    """One provision-sized piece of a wording, as ``/fragmenty`` returns it."""

    id: int
    #: ``/eli/cz/sb/2012/89/2014-01-01/dokument/norma/cast_3/.../par_1180/odst_1``
    eli: str
    #: ``kodTypuFragmentu``: ``Paragraf``, ``Odstavec_Dc``, ``Pismeno``, ``Cast``, ...
    kind: str
    #: ``zkracenaCitace``, e.g. ``§ 1180 odst. 1 zákona č. 89/2012 Sb.`` Kept for logs and
    #: for cross-checking the ELI-derived key; never parsed for the key itself.
    citation: str
    #: ``xhtml`` flattened to plain text.
    text: str
    section: str | None = None
    subsec: str | None = None


def act_eli(act_no: str) -> str:
    """``'89/2012'`` -> ``'/eli/cz/sb/2012/89'``.

    Refuses anything that is not *number/year*. The ELI is the only thing addressing the
    statute, so a silently reordered or padded act number would fetch a different act and
    every wording downstream would be wrong about which rule it quotes.
    """
    match = _ACT_NO_RE.match(act_no.strip())
    if match is None:
        raise ValueError(
            f"{act_no!r} is not an act number in the form the Sbírka prints it, "
            "'number/year' such as '89/2012'."
        )
    number, year = match.groups()
    return f"{ELI_PREFIX}/{year}/{number}"


def wording_stale_url(act_no: str, on: dt.date | None = None) -> str:
    """The staleUrl for an act, optionally for the wording in force on ``on``.

    ``on`` should normally be a wording's own ``valid_from`` rather than "today": the URL
    is the cache key, so asking for today's date would open a fresh cache entry every day
    for a wording that has not changed, and CLAUDE.md rule 5's "a re-run issues zero
    requests" would quietly stop holding.
    """
    base = act_eli(act_no)
    return base if on is None else f"{base}/{on.isoformat()}"


def api_url(stale_url: str, *segments: str, **query: object) -> str:
    """Build one ``/dokumenty-sbirky`` URL. The staleUrl is URL-encoded into the path.

    The API requires the staleUrl to start with ``/`` and to arrive percent-encoded as a
    *single* path segment, which is why ``safe=''``.
    """
    if not stale_url.startswith("/"):
        raise ValueError(
            f"staleUrl {stale_url!r} must start with '/'; the API answers otherwise with "
            "NEPLATNE_STALE_URL."
        )
    path = "/".join((quote(stale_url, safe=""), *segments))
    url = f"{API_BASE_URL}/dokumenty-sbirky/{path}"
    if query:
        pairs = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in query.items())
        url = f"{url}?{pairs}"
    return url


def fetch_api(fetcher: Fetcher, url: str) -> Any:
    """One cache-first GET, decoded as JSON. Rate limiting and the allowlist come from
    :class:`~jg.crawl.base.Fetcher`; nothing here re-implements them."""
    body = fetcher.get(url)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise EsbirkaApiError(
            f"{url} did not return JSON ({exc}). First 200 characters: {body[:200]!r}"
        ) from exc


def resolve_document_id(fetcher: Fetcher, act_no: str, on: dt.date | None = None) -> int:
    """``/id``: the document id of the wording in force on ``on``. Plain integer body."""
    url = api_url(wording_stale_url(act_no, on), "id")
    body = fetcher.get(url).strip()
    try:
        return int(body)
    except ValueError as exc:
        raise EsbirkaApiError(f"{url} returned {body[:120]!r}, expected a document id") from exc


def fetch_wording_detail(fetcher: Fetcher, document_id: int) -> dict[str, Any]:
    """``/detail-zneni``: the act number, title and validity window of one wording."""
    payload = fetch_api(fetcher, f"{API_BASE_URL}/dokumenty-sbirky/{document_id}/detail-zneni")
    if not isinstance(payload, dict):
        raise EsbirkaApiError(
            f"detail-zneni for document {document_id} returned "
            f"{type(payload).__name__}, expected an object"
        )
    return payload


def act_no_from_detail(detail: Mapping[str, Any]) -> str:
    """``{'citace': '89/2012 Sb.'}`` -> ``'89/2012'``.

    The act number is read back off the API rather than echoed from the request, so that
    ``provision.act_no`` carries what e-Sbírka calls the statute. CLAUDE.md rule 1: the
    identifier comes from the source, not from us.
    """
    citation = detail.get("citace")
    if not isinstance(citation, str) or not citation.strip():
        raise EsbirkaApiError(
            "detail-zneni carries no `citace`, so the act number cannot be confirmed "
            "against the source. Refusing to fall back on the requested one."
        )
    act_no = citation.strip().removesuffix("Sb.").strip().rstrip(",").strip()
    if _ACT_NO_RE.match(act_no) is None:
        raise EsbirkaApiError(
            f"detail-zneni `citace` was {citation!r}, which does not reduce to a "
            "'number/year' act number."
        )
    return act_no


def _api_date(value: object) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.date):
        return value
    text = str(value)
    # detail-zneni sends dates as plain `YYYY-MM-DD`, but `datumCasVyhlaseni` is a full
    # offset timestamp; taking the date part of either is safe and never invents a day.
    return dt.date.fromisoformat(text[:10])


def parse_history(payload: Any, act_no: str) -> list[Wording]:
    """Parse a ``/historie`` response into :class:`Wording` values, ascending. Pure."""
    if not isinstance(payload, dict) or not isinstance(payload.get("historie"), list):
        raise EsbirkaApiError(
            "historie response carries no `historie` list; got "
            f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}"
        )
    wordings: list[Wording] = []
    for index, raw in enumerate(payload["historie"]):
        if not isinstance(raw, dict):
            raise EsbirkaApiError(f"historie entry {index} is {type(raw).__name__}")
        valid_from = _api_date(raw.get("datumUcinnostiZneniOd"))
        if valid_from is None:
            raise EsbirkaApiError(
                f"historie entry {index} has no datumUcinnostiZneniOd; a wording with no "
                "start date cannot be placed on a timeline"
            )
        novely = raw.get("novely")
        amendments = tuple(
            str(item["kodDokumentuSbirky"])
            for item in (novely if isinstance(novely, list) else ())
            if isinstance(item, dict) and item.get("kodDokumentuSbirky")
        )
        wordings.append(
            Wording(
                act_no=act_no,
                valid_from=valid_from,
                valid_to=_api_date(raw.get("datumUcinnostiZneniDo")),
                kind=str(raw.get("typZneni") or "NEZNAME"),
                stale_url=raw.get("staleUrl") if isinstance(raw.get("staleUrl"), str) else None,
                amendments=amendments,
            )
        )
    return sorted(wordings, key=lambda wording: wording.valid_from)


def fetch_history(fetcher: Fetcher, act_no: str) -> list[Wording]:
    """Every wording of one act, ascending by ``valid_from``.

    One request per act. This is the cheap half of section 10: it answers "when did this
    statute change" without downloading a single word of statutory text.
    """
    payload = fetch_api(fetcher, api_url(act_eli(act_no), "historie"))
    return parse_history(payload, act_no)


def wording_in_force(wordings: Iterable[Wording], on: dt.date) -> Wording | None:
    """The one wording in force on ``on``, or ``None``. Pure.

    The same predicate as ``ProvisionRepository.VERSION_IN_FORCE`` —
    ``valid_from <= on and (valid_to is null or valid_to >= on)`` — so that "which wording
    did we fetch" and "which version does the engine read" cannot drift apart.
    """
    matching = [
        wording
        for wording in wordings
        if wording.valid_from <= on and (wording.valid_to is None or wording.valid_to >= on)
    ]
    if not matching:
        return None
    return max(matching, key=lambda wording: wording.valid_from)


def fragment_text(xhtml: str) -> str:
    """The wording carried by one fragment's ``xhtml``, as plain text.

    ``<var>(1)</var>`` is the odstavec marker and is kept — it is part of how the provision
    reads. ``<czechvoc-termin>`` wrappers around defined terms are dropped, leaving the
    term itself. Whitespace is normalised, because the evidence-span gate in
    ``jg.provisions`` compares after exactly this normalisation.
    """
    if not xhtml:
        return ""
    return normalise_ws(HTMLParser(xhtml).text(deep=True, separator=""))


def fragment_keys(eli: str) -> tuple[str | None, str | None]:
    """``(section, subsec)`` for one fragment ELI, or ``(None, None)``.

    Read out of the ELI's own ``par_`` / ``odst_`` segments rather than out of the printed
    citation: the path is the API's structural key, so it cannot disagree with itself the
    way a formatted string can. A segment that is not a plausible section number is
    reported as unkeyed rather than coerced.
    """
    par = _PAR_SEGMENT_RE.search(eli)
    if par is None:
        return None, None
    section = par.group(1)
    if _SECTION_RE.match(section) is None:
        log.debug("fragment ELI %s has an unrecognised section segment %r", eli, section)
        return None, None
    odst = _ODST_SEGMENT_RE.search(eli)
    subsec = odst.group(1) if odst is not None else None
    if subsec is not None and _SECTION_RE.match(subsec) is None:
        log.debug("fragment ELI %s has an unrecognised subsection segment %r", eli, subsec)
        subsec = None
    return section, subsec


def parse_fragment_page(payload: Any) -> tuple[list[Fragment], int]:
    """Parse one ``/fragmenty`` page into ``(fragments, page_count)``. Pure."""
    if not isinstance(payload, dict) or not isinstance(payload.get("seznam"), list):
        raise EsbirkaApiError(
            "fragmenty response carries no `seznam` list; got "
            f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}"
        )
    page_count = payload.get("pocetStranek")
    if not isinstance(page_count, int) or page_count < 1:
        raise EsbirkaApiError(f"fragmenty response has pocetStranek={page_count!r}")
    fragments: list[Fragment] = []
    for raw in payload["seznam"]:
        if not isinstance(raw, dict):
            continue
        eli = raw.get("eli")
        if not isinstance(eli, str):
            continue
        section, subsec = fragment_keys(eli)
        fragments.append(
            Fragment(
                id=int(raw.get("id", 0)),
                eli=eli,
                kind=str(raw.get("kodTypuFragmentu") or ""),
                citation=str(raw.get("zkracenaCitace") or ""),
                text=fragment_text(str(raw.get("xhtml") or "")),
                section=section,
                subsec=subsec,
            )
        )
    return fragments, page_count


def fetch_fragments(
    fetcher: Fetcher,
    wording: Wording,
    *,
    sections: Collection[str] | None = None,
    max_pages: int | None = None,
) -> list[Fragment]:
    """Every fragment of one wording, in document order, optionally only some sections.

    Paging note that costs an act's first three hundred sections if it is got wrong:
    **``cisloStranky`` is 0-based.** ``pocetStranek`` is 11 for the civil code and the
    valid page numbers are 0 through 10; page 1 starts at § 309, and a 1-based loop would
    drop §§ 1–308 without any error.

    With ``sections`` given, pages are read in order and the walk stops as soon as every
    requested section has been seen *and* the page just read did not end inside one — a
    section's fragments are contiguous but may straddle a page boundary. Each page is
    roughly 900 kB, so for a section early in the act this is the difference between four
    requests and eleven.
    """
    wanted = {normalise_section(section) for section in sections} if sections else None
    collected: list[Fragment] = []
    seen: set[str] = set()
    page = 0
    page_count: int | None = None
    while page_count is None or page < page_count:
        payload = fetch_api(
            fetcher, api_url(wording.eli(), "fragmenty", cisloStranky=page)
        )
        fragments, page_count = parse_fragment_page(payload)
        if max_pages is not None:
            page_count = min(page_count, max_pages)
        last_wanted = False
        for fragment in fragments:
            if fragment.section is None:
                continue
            if wanted is not None and fragment.section not in wanted:
                last_wanted = False
                continue
            collected.append(fragment)
            seen.add(fragment.section)
            last_wanted = True
        page += 1
        if wanted is not None and wanted <= seen and not last_wanted:
            break
    if wanted is not None:
        missing = sorted(wanted - seen)
        if missing:
            log.warning(
                "%s wording of %s: no fragment for section(s) %s",
                wording.valid_from,
                wording.act_no,
                ", ".join(missing),
            )
    return collected


def versions_from_fragments(
    wording: Wording,
    fragments: Sequence[Fragment],
    *,
    sections: Collection[str] | None = None,
) -> list[ProvisionVersionRecord]:
    """Assemble one wording's fragments into ``provision_version`` records. Pure.

    Two rows come out of a § that has numbered odstavce: the § as a whole
    (``subsec=None``) and each odstavec separately, because a citation may name either and
    ``provision`` keys them as different rows. The § body is every fragment under
    ``par_N`` in document order — its heading, its odstavce, their písmena — joined by
    newlines; an odstavec body is the fragments under ``par_N/odst_K``. A § whose text sits
    in one unnumbered fragment (``par_N/frag_...``) yields the ``subsec=None`` row only,
    which is exactly how it is cited.
    """
    wanted = {normalise_section(section) for section in sections} if sections else None
    bodies: dict[tuple[str, str | None], list[str]] = {}
    for fragment in fragments:
        if fragment.section is None:
            continue
        if wanted is not None and fragment.section not in wanted:
            continue
        if not fragment.text:
            continue
        bodies.setdefault((fragment.section, None), []).append(fragment.text)
        if fragment.subsec is not None:
            bodies.setdefault((fragment.section, fragment.subsec), []).append(fragment.text)

    records: list[ProvisionVersionRecord] = []
    for (section, subsec), parts in bodies.items():
        records.append(
            ProvisionVersionRecord(
                provision=ProvisionRecord(
                    act_no=wording.act_no, section=section, subsec=subsec
                ),
                body="\n".join(parts),
                valid_from=wording.valid_from,
                valid_to=wording.valid_to,
            )
        )
    return records


def fetch_provision_versions(
    fetcher: Fetcher,
    act_no: str,
    sections: Collection[str],
    *,
    dates: Iterable[dt.date] = (),
    all_wordings: bool = False,
    include_non_timeline: bool = False,
    confirm_act_no: bool = True,
) -> list[ProvisionVersionRecord]:
    """The section 10 fetch: some sections of one act, across the wordings that matter.

    ``dates`` are the dates a *question* is asked about — a decision's ``decided_on`` and
    the report's ``asOf``. Each is resolved to the wording in force on it, and duplicates
    collapse, so asking about two dates inside one wording costs one fetch. ``all_wordings``
    ingests the act's whole effective history instead, which is what makes the stored
    timeline gapless rather than two islands with a hole between them.

    ``confirm_act_no`` reads the act number back off ``/detail-zneni`` and uses *that* as
    ``provision.act_no``, so the stored identifier is the source's own (CLAUDE.md rule 1).
    It costs two extra requests per act and can be turned off for a fetch that is purely
    incremental.
    """
    history = fetch_history(fetcher, act_no)
    if not history:
        raise EsbirkaApiError(f"e-Sbírka reports no wordings for act {act_no}")

    canonical = act_no
    if confirm_act_no:
        document_id = resolve_document_id(fetcher, act_no)
        canonical = act_no_from_detail(fetch_wording_detail(fetcher, document_id))
        if canonical != act_no:
            log.info("act %s is cited by e-Sbírka as %s; storing the latter", act_no, canonical)

    usable = [w for w in history if include_non_timeline or w.on_timeline]
    open_ended = [w for w in usable if w.valid_to is None]
    if len(open_ended) > 1:
        # Exactly one wording may be open: the current one. More than one means a kind
        # NON_TIMELINE_WORDING_KINDS does not know about has slipped in, and build_timeline
        # would clamp it into a plausible-looking window rather than reject it.
        raise EsbirkaApiError(
            f"act {act_no}: {len(open_ended)} wordings have no end date "
            f"({', '.join(f'{w.valid_from} {w.kind}' for w in open_ended)}). At most one — "
            "the current one — may be open. A wording kind that is not a point on a "
            "validity timeline needs adding to NON_TIMELINE_WORDING_KINDS; guessing here "
            "would silently put the wrong wording in force for years."
        )
    if all_wordings:
        chosen = usable
    else:
        chosen = []
        for date in sorted(set(dates)):
            wording = wording_in_force(usable, date)
            if wording is None:
                log.warning("act %s has no wording in force on %s", act_no, date)
                continue
            if wording not in chosen:
                chosen.append(wording)
        chosen.sort(key=lambda wording: wording.valid_from)
    if not chosen:
        raise EsbirkaApiError(
            f"no wording of act {act_no} selected. Pass dates= that fall inside the act's "
            f"history ({history[0].valid_from} .. {history[-1].valid_from}) or "
            "all_wordings=True."
        )

    records: list[ProvisionVersionRecord] = []
    for wording in chosen:
        resolved = wording.model_copy(update={"act_no": canonical})
        fragments = fetch_fragments(fetcher, resolved, sections=sections)
        records.extend(versions_from_fragments(resolved, fragments, sections=sections))
    return records


# ---------------------------------------------------------------------- dump reading


def iter_dump_files(directory: Path | None = None) -> Iterator[Path]:
    """Yield dump files under ``directory`` (default :data:`jg.config.ESBIRKA_DIR`).

    Sorted, so a re-run visits them in the same order and the ingest is reproducible. A
    missing directory yields nothing rather than raising: no dumps is a coverage state,
    not an error, and every other stage of this project reports coverage rather than
    crashing on it.
    """
    root = directory if directory is not None else config.ESBIRKA_DIR
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in DUMP_SUFFIXES:
            yield path


def load_dump(path: Path) -> Any:
    """Read one whole-file JSON / JSON-LD dump. UTF-8, no schema assumptions.

    Refuses files over :data:`MAX_DUMP_BYTES`; ``json.load`` cannot stream, so the
    alternative to refusing is an OOM kill halfway through an ingest.
    """
    size = path.stat().st_size
    if size > MAX_DUMP_BYTES:
        raise CrawlUnavailable(
            f"{path} is {size / 1e6:.0f} MB, over the {MAX_DUMP_BYTES / 1e6:.0f} MB "
            "whole-file limit. json.load has no streaming mode. Convert it to JSON Lines "
            "(one record per line, suffix .jsonl) and iter_dump_records will stream it."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def iter_dump_records(path: Path, limit: int | None = None) -> Iterator[Any]:
    """Yield raw records from one dump file, streaming where the format allows it.

    ``.jsonl`` / ``.ndjson`` are read a line at a time, so an arbitrarily large dump costs
    one record of memory. ``.json`` / ``.jsonld`` are slurped, bounded by
    :data:`MAX_DUMP_BYTES`. ``limit`` bounds the number of records yielded either way, so a
    first look at an unfamiliar dump never has to read all of it.

    Nothing here interprets a record. Mapping one onto
    :class:`ProvisionVersionRecord` is the caller's job, and for e-Sbírka's own schema it
    is the open seam :func:`parse_dump` names.
    """
    yielded = 0
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                if limit is not None and yielded >= limit:
                    return
                try:
                    yield json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_no}: not JSON: {exc}") from exc
                yielded += 1
        return

    payload = load_dump(path)
    for record in _envelope_records(payload):
        if limit is not None and yielded >= limit:
            return
        yield record
        yielded += 1


def _envelope_records(payload: Any) -> Sequence[Any]:
    """The record list inside a dump payload.

    Accepts a bare list, or an object carrying one under ``records`` / ``items`` /
    ``data`` / ``@graph`` (the last being the JSON-LD spelling). Anything else raises
    rather than being guessed at — an envelope key that silently resolves to ``[]`` is how
    an ingest reports "0 provisions" while the file sits there full of them.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("records", "items", "data", "@graph"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        raise ValueError(
            f"dump object has no record list; keys are {', '.join(sorted(payload)) or '(none)'}. "
            "Expected one of: records, items, data, @graph."
        )
    raise ValueError(f"dump payload is {type(payload).__name__}, expected a list or an object")


def parse_local_dump(payload: Any) -> list[ProvisionVersionRecord]:
    """Parse a :data:`LOCAL_FORMAT` payload into provision versions. Pure.

    Each record is either flat::

        {"act_no": "89/2012", "section": "2000", "subsec": "1",
         "body": "...", "valid_from": "2014-01-01", "valid_to": null}

    or nested under ``provision``, which is what ``model_dump(mode="json")`` emits, so a
    dump written by this pipeline reads back unchanged.

    This is the repo's own format. It is **not** an e-Sbírka schema and must not be
    presented as one; see :func:`parse_dump`.
    """
    if isinstance(payload, dict):
        declared = payload.get("format")
        if declared is not None and declared != LOCAL_FORMAT:
            raise ValueError(
                f"dump declares format {declared!r}; this reader understands "
                f"{LOCAL_FORMAT!r} only. A different format needs a mapping written "
                "against a dump someone has actually looked at."
            )
    return [_local_record(raw, index) for index, raw in enumerate(_envelope_records(payload))]


def _local_record(raw: Any, index: int) -> ProvisionVersionRecord:
    if not isinstance(raw, dict):
        raise ValueError(f"record {index} is {type(raw).__name__}, expected an object")
    nested = raw.get("provision")
    source: dict[str, Any] = dict(nested) if isinstance(nested, dict) else raw
    act_no = source.get("act_no")
    section = source.get("section")
    if not isinstance(act_no, str) or not act_no.strip():
        raise ValueError(f"record {index}: missing act_no")
    if section is None or not str(section).strip():
        raise ValueError(f"record {index}: missing section")
    body = raw.get("body")
    if not isinstance(body, str) or not body.strip():
        # An empty body would compare equal to nothing and unequal to everything, so every
        # decision relying on the provision would report a rewording it cannot show.
        raise ValueError(f"record {index}: missing body (the wording is the whole point)")
    if raw.get("valid_from") in (None, ""):
        raise ValueError(f"record {index}: missing valid_from; a version with no start date "
                         "cannot be placed on a timeline")
    return ProvisionVersionRecord(
        provision=ProvisionRecord(
            act_no=act_no.strip(),
            section=normalise_section(str(section)),
            subsec=_normalise_subsec(source.get("subsec")),
        ),
        body=body,
        valid_from=raw["valid_from"],
        valid_to=raw.get("valid_to") or None,
        derogated_by=raw.get("derogated_by") or None,
    )


_PARSER_SEAM = (
    "jg.crawl.esbirka has no parser for e-Sbírka's own dump schema, because no e-Sbírka "
    f"dump has ever been read: {BASE_URL} answers every path — including a nonsense one — "
    f"with the same notice that the service moved to {SUCCESSOR_URL}, which is not "
    "allowlisted. Any key path written here would be invented, and an invented key path "
    "yields wrong statute text or a wrong valid_from, which is a confident AMBER on a "
    "decision that is fine. To close this seam: obtain one dump, map its records onto "
    "ProvisionVersionRecord (act_no as printed e.g. '89/2012'; section including any "
    "letter suffix and without '§'; subsec; body; valid_from; valid_to inclusive), and "
    "pin the mapping with a fixture cut from that dump. Until then use parse_local_dump "
    f"with the {LOCAL_FORMAT} format."
)


def parse_dump(payload: Any) -> Iterator[ProvisionVersionRecord]:
    """Seam for e-Sbírka's own dump schema. Raises :class:`ParserSeam`; see above."""
    raise ParserSeam(_PARSER_SEAM)


# ------------------------------------------------------------------- the timeline

class TimelineError(ValueError):
    """A set of versions cannot be made into a well-formed timeline."""


def group_by_provision(
    records: Iterable[ProvisionVersionRecord],
) -> dict[tuple[str, str, str | None], list[ProvisionVersionRecord]]:
    """Bucket versions by ``(act_no, section, subsec)``, preserving input order."""
    grouped: dict[tuple[str, str, str | None], list[ProvisionVersionRecord]] = {}
    for record in records:
        grouped.setdefault(record.provision.key(), []).append(record)
    return grouped


def build_timeline(
    versions: Iterable[ProvisionVersionRecord],
) -> list[ProvisionVersionRecord]:
    """Close the validity windows of one provision's versions. Pure.

    The invariant this exists to produce, and which ``ProvisionRepository`` assumes without
    checking, is: **for any date, at most one version matches**
    ``valid_from <= date and (valid_to is null or valid_to >= date)``. A dump that gives
    only start dates satisfies that for no date at all — every past version stays open and
    the lateral join's ``order by valid_from desc limit 1`` quietly papers over it until a
    gap or a derogation makes it visible.

    What it does, in order:

    * sorts ascending by ``valid_from``;
    * rejects two versions starting on the same day — that is a data error, and picking
      one would be picking which wording a court applied;
    * coalesces consecutive versions with identical body *and* identical ``derogated_by``,
      keeping the earlier ``valid_from``. A republication that changed nothing must not
      show up as a rewording, because the whole amber hangs on ``v0.body <> v1.body``;
    * closes each window on the day before the next one opens, **unless** the record
      declares an earlier ``valid_to``, which is a real gap (repealed, later re-enacted)
      and is preserved;
    * leaves the last version's ``valid_to`` as declared, normally ``None``.

    A declared ``valid_to`` that runs past the next ``valid_from`` is clamped and logged:
    overlapping windows are the one thing that makes the query return the wrong wording
    outright, so they are corrected rather than propagated.
    """
    ordered = sorted(versions, key=lambda record: record.valid_from)
    if not ordered:
        return []

    keys = {record.provision.key() for record in ordered}
    if len(keys) > 1:
        raise TimelineError(
            f"build_timeline takes one provision's versions; got {len(keys)}: "
            f"{', '.join(str(key) for key in sorted(map(str, keys)))}. "
            "Use group_by_provision first."
        )

    for record in ordered:
        if record.valid_to is not None and record.valid_to < record.valid_from:
            raise TimelineError(
                f"{record.provision.label()}: version valid_from {record.valid_from} is after "
                f"its valid_to {record.valid_to}"
            )

    coalesced: list[ProvisionVersionRecord] = []
    for record in ordered:
        previous = coalesced[-1] if coalesced else None
        if previous is not None and previous.valid_from == record.valid_from:
            raise TimelineError(
                f"{record.provision.label()}: two versions both start on {record.valid_from}. "
                "Which wording a court applied on that day is not decidable from this dump."
            )
        if (
            previous is not None
            and previous.body == record.body
            and previous.derogated_by == record.derogated_by
        ):
            # Same wording republished. Extend the earlier window instead of opening a new
            # one; keep the later declared valid_to, which is the one that ends the run.
            coalesced[-1] = previous.model_copy(update={"valid_to": record.valid_to})
            continue
        coalesced.append(record)

    closed: list[ProvisionVersionRecord] = []
    for index, record in enumerate(coalesced):
        if index == len(coalesced) - 1:
            closed.append(record)
            continue
        next_from = coalesced[index + 1].valid_from
        implied_to = next_from - dt.timedelta(days=1)
        declared_to = record.valid_to
        if declared_to is None or declared_to > implied_to:
            if declared_to is not None:
                log.warning(
                    "%s: version from %s declares valid_to %s, which overlaps the next "
                    "version starting %s; clamping to %s",
                    record.provision.label(),
                    record.valid_from,
                    declared_to,
                    next_from,
                    implied_to,
                )
            closed.append(record.model_copy(update={"valid_to": implied_to}))
        else:
            # An earlier declared end is a genuine gap: repealed, later re-enacted.
            closed.append(record)
    return closed


# ------------------------------------------------------------------------- the host


def probe_open_data(fetcher: Fetcher, paths: Sequence[str] = PROBE_PATHS) -> OpenDataProbe:
    """Re-check whether :data:`BASE_URL` still serves anything but the "we moved" notice.

    Cache-first through the :class:`~jg.crawl.base.Fetcher`, so a re-run issues zero
    network requests (CLAUDE.md rule 5) and the whole check can be repeated offline from
    ``data/raw/ESBIRKA/``. ``opendata.eselpoint.cz`` serves an HTML page at
    ``/robots.txt``, so there are no directives to honour; the fetcher's own 1 req/s
    per-host limit still applies.

    Not called by :func:`crawl`. ``crawl`` refuses eagerly, before touching the network at
    all, so that ``jg crawl ESBIRKA`` reports the blocker in a checkout with no cache and
    no connectivity. This is the function to run when you want to know whether the blocker
    is still true.
    """
    responses: dict[str, tuple[int, int]] = {}
    bodies: list[str] = []
    fully_cached = True
    for path in paths:
        url = BASE_URL + path
        try:
            page = fetcher.fetch(url)
        except FetchError as exc:
            log.info("probe %s: %s", url, exc)
            responses[path] = (0, 0)
            continue
        responses[path] = (page.status, len(page.text))
        bodies.append(page.text)
        fully_cached = fully_cached and page.from_cache

    catch_all = len(bodies) > 1 and len(set(bodies)) == 1
    return OpenDataProbe(
        responses=responses,
        catch_all=catch_all,
        moved_notice=bool(bodies) and all(is_moved_notice(body) for body in bodies),
        fully_cached=fully_cached,
    )


def crawl(
    fetcher: Fetcher,
    limit: int | None = None,
    since: dt.date | None = None,
) -> Iterator[ProvisionVersionRecord]:
    """Refuses, eagerly and without touching the network — and names the route that works.

    There is nothing to enumerate here. Unlike a court, a statute source has no "everything
    since date D" to walk: the live API is addressed by *act*, and which acts to load is a
    decision that belongs to the citations already extracted, not to a crawler. So this
    stays a refusal, and it points at :func:`fetch_provision_versions`, which is what
    milestone M6 actually runs.

    Note the return type: unlike the court modules this yields provision versions, not
    ``RawDecision``. e-Sbírka is a statute source; PLAN.md section 10 sends it to
    ``provision`` and ``provision_version``, which ``jg.provisions`` writes.
    """
    raise CrawlUnavailable(
        "e-Sbírka has no bulk crawl. The old open-data host is gone: "
        f"{BASE_URL} is a catch-all that answers every path — including a deliberately "
        "nonsense one — with the same 41 341-byte notice that the service moved to "
        f"{SUCCESSOR_URL} (verified against /robots.txt, /sitemap.xml, /esel-esb/, "
        "/esb-otevrena-data/, /opendata/, /data/, /eli/, /api/ and that control path, all "
        "byte-identical; re-check with jg.crawl.esbirka.probe_open_data(), cache-first). "
        f"Its successor {API_BASE_URL} was added to the allowlist on explicit user "
        "authorisation on 2026-09-03 — CLAUDE.md rule 6 says stop and ask, and that is what "
        "happened. It is public and is what milestone M6 uses, but it is addressed by act "
        "rather than enumerated, so it is fetched per provision: "
        "`python -m jg.provisions fetch --act 89/2012 --section 1180 --all-wordings`, or "
        "jg.crawl.esbirka.fetch_provision_versions() directly. Local dumps in "
        f"{config.ESBIRKA_DIR} in the {LOCAL_FORMAT} format are still read by "
        "`python -m jg.provisions ingest`."
    )

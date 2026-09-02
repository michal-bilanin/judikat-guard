"""Polite, cache-first HTTP plus the text helpers every court crawler shares.

The whole of CLAUDE.md rule 5 and rule 6 lives in :class:`Fetcher`:

* every fetch is cached to ``data/raw/<COURT>/<sha1(url)>.html`` with a
  ``<sha1(url)>.meta.json`` sidecar, and a cache hit issues **zero** network requests;
  a POST is cached the same way, under ``sha1`` of the URL plus its canonical form
  payload, and its sidecar records that payload so a replayed search is auditable;
* at most one request per second **per host**, enforced by sleeping on the delta since
  the last request to that host (and raised to the host's ``Crawl-delay`` when it asks
  for more);
* ``robots.txt`` is fetched once per host, cached like everything else, and honoured;
* any host outside :data:`jg.config.ALLOWLISTED_HOSTS` is refused, before robots.txt and
  before anything touches the network — including every hop of a redirect chain, which is
  why redirects are followed by hand rather than by httpx.

Nothing here knows about courts. The court modules supply URLs and parse strings.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

from jg import config

__all__ = [
    "CachedPage",
    "CrawlUnavailable",
    "FetchError",
    "FetchFailed",
    "Fetcher",
    "HostNotAllowlisted",
    "ParserSeam",
    "RobotsDisallowed",
    "canonical_form",
    "decode_body",
    "detect_marker",
    "fold_text",
    "normalise_ws",
    "parse_cs_date",
    "robots_lines",
    "text_paragraphs",
]


# --------------------------------------------------------------------------- errors


class FetchError(RuntimeError):
    """Base class for everything :class:`Fetcher` refuses or fails to do."""


class HostNotAllowlisted(FetchError):
    """The URL's host is not in :data:`jg.config.ALLOWLISTED_HOSTS`. CLAUDE.md rule 6."""


class RobotsDisallowed(FetchError):
    """The host's robots.txt forbids this path for our user agent. CLAUDE.md rule 5."""


class FetchFailed(FetchError):
    """The server answered with an error status, or the transport gave up."""


class CrawlUnavailable(FetchError):
    """A source cannot be crawled at all under the project's own rules.

    Raised eagerly by a court module's ``crawl()`` when the blocker is structural (the
    decision database lives on a host nobody has allowlisted, robots.txt disallows every
    path, the open-data host has been retired) rather than transient. The message names
    the blocker so the operator can escalate instead of debugging a parser.
    """


class ParserSeam(NotImplementedError):
    """A parse function whose selectors could not be observed against the live site.

    Deliberately loud. A guessed selector that silently returns ``None`` is worse than an
    import-time-visible gap, because it turns a coverage hole into wrong data.
    """


# ------------------------------------------------------------------------ text utils


_WS_RE = re.compile(r"\s+")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_BLOCK_CLOSE_RE = re.compile(r"(?i)</(p|div|tr|li|h[1-6]|table|blockquote)\s*>")


def normalise_ws(text: str) -> str:
    """Collapse every run of whitespace (including NBSP) to one space and strip."""
    return _WS_RE.sub(" ", text.replace("\xa0", " ")).strip()


def fold_text(text: str) -> str:
    """Lowercase, strip diacritics, collapse whitespace.

    Panel markers are matched against the folded form so that ``rozšířený senát`` and a
    diacritic-stripped ``rozsireny senat`` (both occur in scraped metadata) behave the
    same, and so the marker regexes can stay ASCII.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return normalise_ws(stripped).lower()


def text_paragraphs(html: str, *, selector: str | None = None, min_len: int = 1) -> list[str]:
    """Flatten an HTML fragment into paragraph strings.

    Court sites separate paragraphs with ``<br/><br/>`` at least as often as with ``<p>``,
    so ``<br>`` becomes a newline before parsing and block closes get a blank line. The
    result is 0-based by position; the caller assigns ``decision_paragraph.idx``.
    """
    prepared = _BR_RE.sub("\n", html)
    prepared = _BLOCK_CLOSE_RE.sub(lambda m: "\n" + m.group(0), prepared)
    tree = HTMLParser(prepared)
    for tag in tree.css("script, style"):
        tag.decompose()
    root = tree.css_first(selector) if selector else (tree.body or tree.root)
    if root is None:
        return []
    flat = root.text(deep=True, separator="")
    out: list[str] = []
    for chunk in flat.split("\n"):
        cleaned = normalise_ws(chunk)
        if len(cleaned) >= min_len:
            out.append(cleaned)
    return out


#: Long-form Czech month names, genitive, as they appear in decision headings.
CS_MONTHS: dict[str, int] = {
    "ledna": 1,
    "unora": 2,
    "brezna": 3,
    "dubna": 4,
    "kvetna": 5,
    "cervna": 6,
    "cervence": 7,
    "srpna": 8,
    "zari": 9,
    "rijna": 10,
    "listopadu": 11,
    "prosince": 12,
}

_NUMERIC_DATE_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
_LONG_DATE_RE = re.compile(r"(\d{1,2})\.\s*([a-z]+)\s+(\d{4})")


def parse_cs_date(text: str) -> dt.date:
    """Parse ``9. 7. 2009`` or ``9. července 2009`` out of ``text``.

    Raises ``ValueError`` rather than guessing; a wrong ``decided_on`` silently corrupts
    every provision-in-force lookup downstream (PLAN.md section 10).
    """
    numeric = _NUMERIC_DATE_RE.search(text)
    if numeric:
        day, month, year = (int(g) for g in numeric.groups())
        return dt.date(year, month, day)
    long_form = _LONG_DATE_RE.search(fold_text(text))
    if long_form:
        day_s, month_name, year_s = long_form.groups()
        month = CS_MONTHS.get(month_name)
        if month is not None:
            return dt.date(int(year_s), month, int(day_s))
    raise ValueError(f"no Czech date found in {text!r}")


def detect_marker(
    text: str,
    markers: Iterable[re.Pattern[str]],
    *,
    referral_verbs: re.Pattern[str] | None = None,
    lookback: int = 48,
) -> bool:
    """True when ``text`` asserts a marker rather than merely referring a case onward.

    The distinction is the whole point of panel detection. ``rozšířený senát ... rozhodl``
    is an extended-panel decision; ``senát věc postoupil rozšířenému senátu`` is a normal
    panel handing the question over, and counting it as extended would invent authority
    the decision does not have. Every marker hit whose preceding ``lookback`` characters
    contain a referral verb is discarded; the marker counts only if some hit survives.
    """
    folded = fold_text(text)
    if not folded:
        return False
    for marker in markers:
        for hit in marker.finditer(folded):
            if referral_verbs is None:
                return True
            window = folded[max(0, hit.start() - lookback) : hit.start()]
            if not referral_verbs.search(window):
                return True
    return False


# ----------------------------------------------------------------------- the fetcher


@dataclass(frozen=True, slots=True)
class CachedPage:
    """One fetched (or replayed) page plus the sidecar metadata the crawlers need."""

    url: str
    text: str
    fetched_at: dt.datetime
    status: int
    content_type: str
    path: Path
    from_cache: bool


def _url_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()


def canonical_form(data: Mapping[str, str], *, omit: Collection[str] = ()) -> str:
    """A form payload as one deterministic string, for use in a cache key.

    Sorted by field name, so two runs that build the same 299-field search payload in a
    different dict order hash identically. ``omit`` drops fields whose value changes every
    session and would otherwise make a re-run miss its own cache — an ASP.NET
    ``__RequestVerificationToken`` is the case this exists for. Omitted fields are still
    *sent*, and still recorded in the sidecar; they are only left out of the key.
    """
    skip = set(omit)
    items = sorted((key, value) for key, value in data.items() if key not in skip)
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _post_key(url: str, payload: str, salt: str) -> str:
    material = f"POST\n{url}\n{salt}\n{payload}"
    return hashlib.sha1(material.encode("utf-8"), usedforsecurity=False).hexdigest()


def decode_body(response: httpx.Response) -> str:
    """The response body as text, without letting an odd encoding kill the crawl.

    ``httpx`` decodes with the charset the server declared, and raises when the bytes do
    not match it. ``vyhledavac.nssoud.cz`` does exactly that: ``/DokumentOriginal/Text/``
    announces ``charset=UTF-16`` and then sends UTF-16LE with no byte-order mark, so
    ``response.text`` raises ``UTF-16 stream does not start with BOM`` and takes the whole
    crawl down over one document.

    The declared charset is retried directly (Python's ``utf-16`` codec assumes
    little-endian when no BOM is present, which is what recovers that page), then UTF-8,
    and only then UTF-8 with replacement. Guessing at a *different* legacy codec is
    deliberately not attempted: silently mis-decoded Czech text would be worse than a
    handful of replacement characters, because it would be stored as if it were the
    decision.
    """
    try:
        return response.text
    except (UnicodeError, LookupError):
        pass
    raw = response.content
    declared = response.headers.get("content-type", "").partition("charset=")[2]
    declared = declared.split(";")[0].strip().strip('"').lower()
    for codec in (declared, "utf-8"):
        if not codec:
            continue
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def robots_lines(body: str) -> list[str]:
    """``body`` split into lines with a UTF-8 BOM removed.

    Observed on ``www.nssoud.cz``, whose robots.txt begins with a BOM.
    ``urllib.robotparser`` compares the field name literally, so the BOM turns the leading
    ``User-agent: *`` into an unrecognised directive; every rule under it is then ignored
    because the parser is still in its initial state. The file silently becomes empty, and
    with it that host's ``Crawl-Delay: 10`` and its ``Disallow`` entries. Dropping a host's
    own rate limit through a byte-order mark is precisely what CLAUDE.md rule 5 forbids.
    """
    return body.lstrip("﻿").splitlines()


class Fetcher:
    """Cache-first HTTP for one court's crawl.

    ``court_code`` names the subdirectory under ``data/raw/``; it is a cache namespace,
    nothing more. Instances are not thread-safe, which is fine: the rate limit means one
    request per host per second anyway.
    """

    def __init__(
        self,
        court_code: str,
        delay: float = config.REQUEST_DELAY_SECONDS,
        *,
        raw_dir: Path | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        max_redirects: int = 5,
        user_agent: str = config.USER_AGENT,
        allowlist: frozenset[str] | None = None,
        obey_crawl_delay: bool = True,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.court_code = court_code
        self.delay = delay
        self.cache_dir = (raw_dir if raw_dir is not None else config.RAW_DIR) / court_code
        self.user_agent = user_agent
        self.allowlist = allowlist if allowlist is not None else config.ALLOWLISTED_HOSTS
        self.max_retries = max_retries
        self.max_redirects = max_redirects
        self.obey_crawl_delay = obey_crawl_delay
        self._sleeper = sleeper
        self._clock = clock
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser] = {}
        self._crawl_delay: dict[str, float] = {}

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- cache --------------------------------------------------------------

    def cache_paths(self, url: str) -> tuple[Path, Path]:
        """``(body, sidecar)`` for ``url``. Public so a crawl can report its own cache."""
        return self._paths_for_key(_url_key(url))

    def _paths_for_key(self, key: str) -> tuple[Path, Path]:
        return self.cache_dir / f"{key}.html", self.cache_dir / f"{key}.meta.json"

    def post_cache_paths(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        cache_omit: Collection[str] = (),
        cache_salt: str = "",
    ) -> tuple[Path, Path]:
        """``(body, sidecar)`` for one POST. Same layout as :meth:`cache_paths`."""
        key = _post_key(url, canonical_form(data, omit=cache_omit), cache_salt)
        return self._paths_for_key(key)

    def is_cached(self, url: str) -> bool:
        return self.cache_paths(url)[0].exists()

    def is_post_cached(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        cache_omit: Collection[str] = (),
        cache_salt: str = "",
    ) -> bool:
        return self.post_cache_paths(
            url, data, cache_omit=cache_omit, cache_salt=cache_salt
        )[0].exists()

    def _read_cache(self, url: str, key: str | None = None) -> CachedPage | None:
        body_path, meta_path = self._paths_for_key(key) if key else self.cache_paths(url)
        if not body_path.exists():
            return None
        # errors="replace" rather than a hard failure: a body that was written from an
        # oddly-encoded response must stay replayable, or rule 5's "a re-run issues zero
        # requests" quietly stops holding for exactly the pages that were hardest to get.
        text = body_path.read_text(encoding="utf-8", errors="replace")
        meta: dict[str, object] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        raw_fetched_at = meta.get("fetched_at")
        if isinstance(raw_fetched_at, str):
            fetched_at = dt.datetime.fromisoformat(raw_fetched_at)
        else:
            # A body without a readable sidecar is still a hit. Re-fetching it would
            # break CLAUDE.md rule 5 over a bookkeeping detail.
            fetched_at = dt.datetime.fromtimestamp(body_path.stat().st_mtime, tz=dt.UTC)
        status = meta.get("status")
        content_type = meta.get("content_type")
        return CachedPage(
            url=url,
            text=text,
            fetched_at=fetched_at,
            status=status if isinstance(status, int) else 200,
            content_type=content_type if isinstance(content_type, str) else "",
            path=body_path,
            from_cache=True,
        )

    def _write_cache(
        self,
        url: str,
        text: str,
        status: int,
        content_type: str,
        *,
        key: str | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> CachedPage:
        body_path, meta_path = self._paths_for_key(key) if key else self.cache_paths(url)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        fetched_at = dt.datetime.now(tz=dt.UTC)
        # A body recovered by decode_body may carry replacement characters or lone
        # surrogates; writing must not raise on them, for the same reason reading does not.
        body_path.write_text(text, encoding="utf-8", errors="replace")
        meta: dict[str, object] = {
            "url": url,
            "fetched_at": fetched_at.isoformat(),
            "status": status,
            "content_type": content_type,
        }
        if extra:
            meta.update(extra)
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return CachedPage(
            url=url,
            text=text,
            fetched_at=fetched_at,
            status=status,
            content_type=content_type,
            path=body_path,
            from_cache=False,
        )

    # -- policy -------------------------------------------------------------

    def _require_allowlisted(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        if host not in self.allowlist:
            raise HostNotAllowlisted(
                f"refusing to fetch {url!r}: host {host!r} is not allowlisted. "
                "CLAUDE.md rule 6 lists the only permitted hosts "
                f"({', '.join(sorted(self.allowlist))}); adding one is a decision to "
                "escalate to the user, not a code change to make."
            )
        return host

    def _robots_for(self, url: str, host: str) -> RobotFileParser:
        parser = self._robots.get(host)
        if parser is not None:
            return parser
        parser = RobotFileParser()
        split = urlsplit(url)
        robots_url = f"{split.scheme}://{split.netloc}/robots.txt"
        cached = self._read_cache(robots_url)
        if cached is not None:
            status, body = cached.status, cached.text
        else:
            try:
                response = self._request(robots_url, host)
            except FetchError:
                # Unreachable robots.txt is treated as "disallow": we are a guest here.
                parser.disallow_all = True
                self._robots[host] = parser
                return parser
            status, body = response.status_code, decode_body(response)
            self._write_cache(
                robots_url,
                body,
                status,
                response.headers.get("content-type", ""),
            )
        if status >= 500:
            parser.disallow_all = True
        elif status >= 400:
            parser.allow_all = True
        else:
            parser.parse(robots_lines(body))
            parser.modified()
        self._robots[host] = parser
        declared = parser.crawl_delay(self.user_agent) if self.obey_crawl_delay else None
        if declared is not None:
            self._crawl_delay[host] = float(declared)
        return parser

    def _require_robots_allows(self, url: str, host: str) -> None:
        parser = self._robots_for(url, host)
        if not parser.can_fetch(self.user_agent, url):
            raise RobotsDisallowed(
                f"refusing to fetch {url!r}: {host} robots.txt disallows it for "
                f"user agent {self.user_agent!r}. CLAUDE.md rule 5."
            )

    def _effective_delay(self, host: str) -> float:
        return max(self.delay, self._crawl_delay.get(host, 0.0))

    def _throttle(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            wait = self._effective_delay(host) - (self._clock() - last)
            if wait > 0:
                self._sleeper(wait)
        self._last_request_at[host] = self._clock()

    # -- network ------------------------------------------------------------

    def _get_following_redirects(self, url: str, host: str) -> httpx.Response:
        """One throttled GET, following redirects by hand.

        ``follow_redirects=False`` is deliberate. Letting httpx follow the chain would
        have it *issue* the request to the next hop before this class ever sees the URL,
        so a redirect off ``nssoud.cz`` would already have been fetched by the time the
        allowlist rejected it. Each hop is allowlisted before it is requested, and each
        hop is throttled against its own host.
        """
        current_url, current_host = url, host
        for _ in range(self.max_redirects + 1):
            self._throttle(current_host)
            response = self._client.get(
                current_url,
                headers={"User-Agent": self.user_agent},
                follow_redirects=False,
            )
            if not response.is_redirect:
                return response
            following = response.next_request
            location = (
                str(following.url)
                if following is not None
                else urljoin(current_url, response.headers.get("location", ""))
            )
            current_url = location
            current_host = self._require_allowlisted(current_url)
        raise FetchFailed(f"more than {self.max_redirects} redirects starting at {url!r}")

    def _request(self, url: str, host: str) -> httpx.Response:
        """One GET with a bounded retry on 5xx and transport errors."""
        return self._attempt(lambda: self._get_following_redirects(url, host), url)

    def _post_following_redirects(
        self, url: str, host: str, data: Mapping[str, str]
    ) -> httpx.Response:
        """One throttled POST. A redirect answer is followed as a GET, hop by hop.

        Same reasoning as :meth:`_get_following_redirects`: every hop is allowlisted before
        it is requested. A 303 (and, by universal browser practice, a 301/302) after a POST
        becomes a GET, which is why the chain hands over to the GET path rather than
        re-posting the payload to a host that may not have asked for it.
        """
        self._throttle(host)
        response = self._client.post(
            url,
            data=dict(data),
            headers={"User-Agent": self.user_agent},
            follow_redirects=False,
        )
        if not response.is_redirect:
            return response
        following = response.next_request
        location = (
            str(following.url)
            if following is not None
            else urljoin(url, response.headers.get("location", ""))
        )
        next_host = self._require_allowlisted(location)
        return self._get_following_redirects(location, next_host)

    def _post_request(self, url: str, host: str, data: Mapping[str, str]) -> httpx.Response:
        """One POST with the same bounded retry policy as :meth:`_request`."""
        return self._attempt(lambda: self._post_following_redirects(url, host, data), url)

    def _attempt(self, send: Callable[[], httpx.Response], url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = send()
            except httpx.HTTPError as exc:  # transport, timeout
                last_error = exc
                continue
            if response.status_code >= 500 and attempt < self.max_retries:
                last_error = FetchFailed(f"{url} returned {response.status_code}")
                continue
            return response
        raise FetchFailed(f"giving up on {url!r} after {self.max_retries + 1} attempts") from (
            last_error
        )

    # -- public api ---------------------------------------------------------

    def fetch(self, url: str) -> CachedPage:
        """Return ``url`` as a :class:`CachedPage`, from disk when it is already there.

        Order matters: the allowlist is checked first and always, then the on-disk cache,
        then robots.txt. Consulting robots before the cache would make a re-run issue a
        network request for pages it already holds, which CLAUDE.md rule 5 forbids; a
        cached body could only have got there by passing robots in the first place.
        """
        host = self._require_allowlisted(url)
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        self._require_robots_allows(url, host)
        response = self._request(url, host)
        if response.status_code >= 400:
            raise FetchFailed(f"{url!r} returned HTTP {response.status_code}")
        return self._write_cache(
            url,
            decode_body(response),
            response.status_code,
            response.headers.get("content-type", ""),
        )

    def get(self, url: str) -> str:
        """The response body for ``url``. Cache first; a hit makes no network request."""
        return self.fetch(url).text

    def fetch_post(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        cache_omit: Collection[str] = (),
        cache_salt: str = "",
    ) -> CachedPage:
        """POST ``data`` to ``url``, from disk when that exact request was made before.

        Same order and the same guarantees as :meth:`fetch`: allowlist first and always,
        then the on-disk cache, then robots.txt, then the per-host rate limit. A search
        whose results are already cached therefore issues zero network requests, which is
        what makes a multi-day crawl resumable.

        ``cache_omit`` names fields that are sent but left out of the cache key; see
        :func:`canonical_form`. ``cache_salt`` makes an otherwise identical request a
        distinct cache entry, which is how a caller takes a *second sample* of an endpoint
        whose paging is not deterministic — without it, re-asking the same question could
        only ever replay the first answer.

        The sidecar records the full payload under ``"form"``, so a cached search can be
        audited later without re-deriving what was asked.
        """
        host = self._require_allowlisted(url)
        payload = canonical_form(data, omit=cache_omit)
        key = _post_key(url, payload, cache_salt)
        cached = self._read_cache(url, key)
        if cached is not None:
            return cached
        self._require_robots_allows(url, host)
        response = self._post_request(url, host, data)
        if response.status_code >= 400:
            raise FetchFailed(f"POST {url!r} returned HTTP {response.status_code}")
        return self._write_cache(
            url,
            decode_body(response),
            response.status_code,
            response.headers.get("content-type", ""),
            key=key,
            extra={
                "method": "POST",
                "form": dict(sorted(data.items())),
                "cache_key_omits": sorted(cache_omit),
                "cache_salt": cache_salt,
            },
        )

    def post(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        cache_omit: Collection[str] = (),
        cache_salt: str = "",
    ) -> str:
        """The response body for one POST. Cache first; a hit makes no network request."""
        return self.fetch_post(
            url, data, cache_omit=cache_omit, cache_salt=cache_salt
        ).text

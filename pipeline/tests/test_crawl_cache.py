"""Fetcher behaviour: the on-disk cache, the allowlist, robots.txt, the rate limit.

CLAUDE.md rules 5 and 6 are asserted here and nowhere else. Every test drives an
``httpx.MockTransport``; nothing in this file may touch the network, and the counters
prove it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jg import crawl as crawl_pkg
from jg.crawl.base import (
    Fetcher,
    FetchFailed,
    HostNotAllowlisted,
    RobotsDisallowed,
    canonical_form,
)

# Real allowlisted host, invented paths. Nothing here is a legal identifier.
HOST = "https://nalus.usoud.cz"
PAGE_URL = f"{HOST}/Search/GetText.aspx?sz=TEST-cache-1"
OTHER_URL = f"{HOST}/Search/GetText.aspx?sz=TEST-cache-2"
ROBOTS_URL = f"{HOST}/robots.txt"

BODY = "<html><body><p>caching probe</p></body></html>"


class Recorder:
    """A MockTransport handler that counts and can be told what to answer."""

    def __init__(self, robots: str = "User-agent: *\nAllow: /\n", robots_status: int = 200):
        self.calls: list[str] = []
        #: ``(url, decoded form payload)`` for every POST, so a test can prove that a
        #: field left out of the cache *key* was still sent on the wire.
        self.posts: list[tuple[str, str]] = []
        self.robots = robots
        self.robots_status = robots_status
        self.page_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if request.method == "POST":
            self.posts.append((url, request.content.decode("utf-8")))
        if url.endswith("/robots.txt"):
            return httpx.Response(
                self.robots_status,
                text=self.robots,
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(
            self.page_status,
            text=BODY,
            headers={"content-type": "text/html; charset=utf-8"},
        )


def make_fetcher(tmp_path, handler, **kwargs) -> Fetcher:
    """A Fetcher wired to a mock transport. Sleeps are no-ops unless a test asks for them."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs.setdefault("sleeper", lambda _seconds: None)
    return Fetcher("TEST-US", 1.0, raw_dir=tmp_path, client=client, **kwargs)


class ExplodingHandler:
    """Any network call at all is a test failure."""

    def __call__(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"network request issued for {request.url}")


# ------------------------------------------------------------------ cache, rule 5


def test_second_get_is_served_from_cache_with_zero_requests(tmp_path):
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        assert fetcher.get(PAGE_URL) == BODY
    # robots.txt plus the page, and nothing else.
    assert handler.calls == [ROBOTS_URL, PAGE_URL]

    # A fresh Fetcher over the same data/raw tree: re-running the crawl must not touch
    # the network at all, not even for robots.txt.
    with make_fetcher(tmp_path, ExplodingHandler()) as rerun:
        assert rerun.get(PAGE_URL) == BODY
        assert rerun.is_cached(PAGE_URL)


def test_cache_writes_body_and_sidecar_metadata(tmp_path):
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        page = fetcher.fetch(PAGE_URL)

    body_path, meta_path = fetcher.cache_paths(PAGE_URL)
    assert body_path.parent == tmp_path / "TEST-US"
    assert body_path.read_text(encoding="utf-8") == BODY
    assert page.from_cache is False

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert set(meta) == {"url", "fetched_at", "status", "content_type"}
    assert meta["url"] == PAGE_URL
    assert meta["status"] == 200
    assert meta["content_type"].startswith("text/html")
    assert meta["fetched_at"].startswith(str(page.fetched_at.year))


def test_cached_page_survives_a_missing_sidecar(tmp_path):
    """Losing the bookkeeping file must not cost a network request."""
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.get(PAGE_URL)
        _, meta_path = fetcher.cache_paths(PAGE_URL)
        meta_path.unlink()
        # robots is still in memory here; use a fresh fetcher to prove disk-only replay.
    with make_fetcher(tmp_path, ExplodingHandler()) as rerun:
        assert rerun.get(PAGE_URL) == BODY


# ------------------------------------------------------------- cached POST, rule 5

# A search-shaped payload. The token stands in for ASP.NET Core's antiforgery field: it
# changes every session, so it is sent but kept out of the cache key.
SEARCH_URL = f"{HOST}/Search/Results.aspx?formular=1"
SEARCH_FORM = {"datumOd": "1.1.2024", "datumDo": "5.1.2024", "__RequestVerificationToken": "aaa"}
VOLATILE = ("__RequestVerificationToken",)


def test_second_post_of_the_same_search_is_served_from_cache(tmp_path):
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        assert fetcher.post(SEARCH_URL, SEARCH_FORM) == BODY
    assert handler.calls == [ROBOTS_URL, SEARCH_URL]

    with make_fetcher(tmp_path, ExplodingHandler()) as rerun:
        assert rerun.post(SEARCH_URL, SEARCH_FORM) == BODY
        assert rerun.is_post_cached(SEARCH_URL, SEARCH_FORM)


def test_post_cache_key_covers_the_payload_not_just_the_url(tmp_path):
    """Two searches at one URL are two cache entries, or paging replays page 1 forever."""
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.post(SEARCH_URL, SEARCH_FORM)
        fetcher.post(SEARCH_URL, {**SEARCH_FORM, "datumOd": "6.1.2024"})
    assert handler.calls.count(SEARCH_URL) == 2


def test_post_cache_key_is_independent_of_field_order(tmp_path):
    handler = Recorder()
    reordered = dict(reversed(list(SEARCH_FORM.items())))
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.post(SEARCH_URL, SEARCH_FORM)
        fetcher.post(SEARCH_URL, reordered)
    assert handler.calls.count(SEARCH_URL) == 1


def test_a_get_and_a_post_to_one_url_do_not_share_a_cache_entry(tmp_path):
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.get(SEARCH_URL)
        fetcher.post(SEARCH_URL, SEARCH_FORM)
    assert handler.calls.count(SEARCH_URL) == 2


def test_omitted_fields_are_sent_but_stay_out_of_the_cache_key(tmp_path):
    """A per-session token must not stop a re-run from hitting its own cache.

    ASP.NET Core mints a fresh ``__RequestVerificationToken`` on every visit to the form.
    Keyed on the whole payload, the second run would differ in exactly that field, miss
    the cache, and re-fetch every page of the crawl — the failure CLAUDE.md rule 5 exists
    to prevent. The field is still posted, which this asserts on the wire.
    """
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.post(SEARCH_URL, SEARCH_FORM, cache_omit=VOLATILE)
        fetcher.post(
            SEARCH_URL,
            {**SEARCH_FORM, "__RequestVerificationToken": "a-different-token"},
            cache_omit=VOLATILE,
        )
    assert handler.calls.count(SEARCH_URL) == 1
    assert len(handler.posts) == 1
    assert "__RequestVerificationToken=aaa" in handler.posts[0][1]


def test_cache_salt_takes_a_second_sample_of_the_same_request(tmp_path):
    """The knob for an endpoint whose paging is not deterministic.

    Without it, asking the same question twice could only ever replay the first answer,
    so a result set the server pages inconsistently could never be completed.
    """
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.post(SEARCH_URL, SEARCH_FORM)
        fetcher.post(SEARCH_URL, SEARCH_FORM, cache_salt="pass1")
    assert handler.calls.count(SEARCH_URL) == 2

    # Both samples are on disk, so the re-run replays the same union with no requests.
    with make_fetcher(tmp_path, ExplodingHandler()) as rerun:
        assert rerun.post(SEARCH_URL, SEARCH_FORM) == BODY
        assert rerun.post(SEARCH_URL, SEARCH_FORM, cache_salt="pass1") == BODY


def test_post_sidecar_records_the_payload_that_was_sent(tmp_path):
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.post(SEARCH_URL, SEARCH_FORM, cache_omit=VOLATILE, cache_salt="pass1")
        _body, meta_path = fetcher.post_cache_paths(
            SEARCH_URL, SEARCH_FORM, cache_omit=VOLATILE, cache_salt="pass1"
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["method"] == "POST"
    assert meta["url"] == SEARCH_URL
    # A cached search is auditable: what was asked, and what was left out of the key.
    assert meta["form"] == SEARCH_FORM
    assert meta["cache_key_omits"] == list(VOLATILE)
    assert meta["cache_salt"] == "pass1"


def test_a_get_sidecar_gains_no_post_fields(tmp_path):
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.get(PAGE_URL)
        _body, meta_path = fetcher.cache_paths(PAGE_URL)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "form" not in meta and "method" not in meta


def test_post_to_a_non_allowlisted_host_is_refused_before_any_request(tmp_path):
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        with pytest.raises(HostNotAllowlisted):
            fetcher.post("https://example.com/Search", SEARCH_FORM)
    assert handler.calls == []


def test_post_honours_robots(tmp_path):
    handler = Recorder(robots="User-agent: *\nDisallow: /\n")
    with make_fetcher(tmp_path, handler) as fetcher:
        with pytest.raises(RobotsDisallowed):
            fetcher.post(SEARCH_URL, SEARCH_FORM)
    assert handler.calls == [ROBOTS_URL]
    assert not fetcher.is_post_cached(SEARCH_URL, SEARCH_FORM)


def test_post_is_rate_limited_like_a_get(tmp_path):
    slept: list[float] = []
    handler = Recorder()
    with make_fetcher(tmp_path, handler, sleeper=slept.append) as fetcher:
        fetcher.post(SEARCH_URL, SEARCH_FORM)
        before = len(slept)
        fetcher.post(SEARCH_URL, {**SEARCH_FORM, "datumOd": "6.1.2024"})
    assert len(slept) > before


def test_post_error_status_is_not_cached(tmp_path):
    handler = Recorder()
    handler.page_status = 400
    with make_fetcher(tmp_path, handler, sleeper=lambda _: None) as fetcher:
        with pytest.raises(FetchFailed):
            fetcher.post(SEARCH_URL, SEARCH_FORM)
    assert not fetcher.is_post_cached(SEARCH_URL, SEARCH_FORM)


def test_a_post_redirected_off_the_allowlist_is_refused_before_the_hop(tmp_path):
    calls: list[str] = []

    def redirecting(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(303, headers={"location": "https://example.com/elsewhere"})

    with make_fetcher(tmp_path, redirecting) as fetcher:
        with pytest.raises(HostNotAllowlisted):
            fetcher.post(SEARCH_URL, SEARCH_FORM)
    assert not any("example.com" in url for url in calls)


def test_canonical_form_is_order_independent_and_honours_omit():
    assert canonical_form({"b": "2", "a": "1"}) == canonical_form({"a": "1", "b": "2"})
    assert canonical_form({"a": "1", "t": "x"}, omit=("t",)) == canonical_form({"a": "1"})


# ------------------------------------------------------------------ odd encodings


def test_a_body_that_contradicts_its_declared_charset_does_not_kill_the_crawl(tmp_path):
    """``vyhledavac.nssoud.cz/DokumentOriginal/Text/`` says UTF-16 and sends no BOM.

    ``httpx`` raises "UTF-16 stream does not start with BOM" on that, which would take
    down a whole crawl over one document. The declared codec is retried directly, which
    recovers the text intact rather than mangling it.
    """
    text = "Kasační stížnost se zamítá."

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200,
            content=text.encode("utf-16-le"),
            headers={"content-type": "text/html; charset=UTF-16"},
        )

    with make_fetcher(tmp_path, handler) as fetcher:
        assert fetcher.get(PAGE_URL) == text

    # And it round-trips through the cache, so the re-run gets the same string.
    with make_fetcher(tmp_path, ExplodingHandler()) as rerun:
        assert rerun.get(PAGE_URL) == text


def test_undecodable_bytes_become_replacement_characters_not_an_exception(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200,
            content=b"\xff\xfe\x00rozhodnut\xed",
            headers={"content-type": "text/plain; charset=x-not-a-codec"},
        )

    with make_fetcher(tmp_path, handler) as fetcher:
        body = fetcher.get(PAGE_URL)
    assert "rozhodnut" in body
    with make_fetcher(tmp_path, ExplodingHandler()) as rerun:
        assert rerun.get(PAGE_URL) == body


# --------------------------------------------------------------- allowlist, rule 6


def test_non_allowlisted_host_is_refused_before_any_request(tmp_path):
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        with pytest.raises(HostNotAllowlisted) as excinfo:
            fetcher.get("https://example.com/rozhodnuti")
    assert handler.calls == []
    message = str(excinfo.value)
    assert "example.com" in message
    assert "CLAUDE.md rule 6" in message


def test_allowlist_is_checked_before_robots(tmp_path):
    """A refused host must not even provoke a robots.txt fetch.

    The host used here is a *sibling subdomain* of an allowlisted one on purpose: the
    allowlist is a set of literal hostnames, and nothing about ``nssoud.cz`` being in it
    may be read as implying its subdomains. ``vyhledavac.nssoud.cz`` stood here until it
    was allowlisted by hand on 2026-09-02; it is a real host now, so the point is made
    with one that is not.
    """
    handler = Recorder(robots="User-agent: *\nDisallow: /\n")
    with make_fetcher(tmp_path, handler) as fetcher:
        with pytest.raises(HostNotAllowlisted):
            fetcher.get("https://TEST-neexistuje.nssoud.cz/")
    assert handler.calls == []


def test_redirect_off_the_allowlist_is_refused_before_the_hop_is_fetched(tmp_path):
    """Refusing *after* httpx already followed the hop would be too late for rule 6."""
    calls: list[str] = []

    def redirecting(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if url.startswith("https://example.com"):
            return httpx.Response(200, text=BODY)
        return httpx.Response(302, headers={"location": "https://example.com/elsewhere"})

    with make_fetcher(tmp_path, redirecting) as fetcher:
        with pytest.raises(HostNotAllowlisted):
            fetcher.get(PAGE_URL)

    assert not any("example.com" in url for url in calls)
    assert not fetcher.is_cached(PAGE_URL)


def test_redirect_within_the_allowlist_is_followed(tmp_path):
    def redirecting(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if url == PAGE_URL:
            return httpx.Response(302, headers={"location": OTHER_URL})
        return httpx.Response(200, text=BODY, headers={"content-type": "text/html"})

    with make_fetcher(tmp_path, redirecting) as fetcher:
        assert fetcher.get(PAGE_URL) == BODY
        # Cached under the URL that was asked for, so a re-run of the same crawl replays.
        assert fetcher.is_cached(PAGE_URL)


def test_a_redirect_loop_gives_up(tmp_path):
    def looping(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(302, headers={"location": PAGE_URL})

    with make_fetcher(tmp_path, looping, max_retries=0, max_redirects=2) as fetcher:
        with pytest.raises(FetchFailed, match="redirect"):
            fetcher.get(PAGE_URL)


# ------------------------------------------------------------------ robots, rule 5


def test_robots_disallow_is_honoured(tmp_path):
    handler = Recorder(robots="User-agent: *\nDisallow: /\n")
    with make_fetcher(tmp_path, handler) as fetcher:
        with pytest.raises(RobotsDisallowed) as excinfo:
            fetcher.get(PAGE_URL)
    assert handler.calls == [ROBOTS_URL]
    assert "robots.txt" in str(excinfo.value)
    assert not fetcher.is_cached(PAGE_URL)


def test_robots_is_fetched_once_per_host(tmp_path):
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.get(PAGE_URL)
        fetcher.get(OTHER_URL)
    assert handler.calls.count(ROBOTS_URL) == 1


def test_missing_robots_allows_everything(tmp_path):
    handler = Recorder(robots="not found", robots_status=404)
    with make_fetcher(tmp_path, handler) as fetcher:
        assert fetcher.get(PAGE_URL) == BODY


def test_robots_with_a_utf8_bom_is_still_honoured(tmp_path):
    """www.nssoud.cz serves robots.txt with a BOM. urllib drops the whole file over it.

    Without the BOM strip in ``base.robots_lines`` the leading ``User-agent`` line is
    unrecognised, the parser never leaves its initial state, and every rule beneath it —
    including that host's ``Crawl-Delay: 10`` — is silently discarded.
    """
    handler = Recorder(robots="﻿User-agent: *\nCrawl-Delay: 10\nDisallow: /Search/\n")
    with make_fetcher(tmp_path, handler) as fetcher:
        with pytest.raises(RobotsDisallowed):
            fetcher.get(PAGE_URL)
    assert handler.calls == [ROBOTS_URL]


def test_declared_crawl_delay_survives_a_bom(tmp_path):
    slept: list[float] = []
    handler = Recorder(robots="﻿User-agent: *\nCrawl-Delay: 10\nAllow: /\n")
    with make_fetcher(tmp_path, handler, sleeper=slept.append) as fetcher:
        fetcher.get(PAGE_URL)
        fetcher.get(OTHER_URL)
    assert max(slept) > 5.0


def test_declared_crawl_delay_raises_the_gap(tmp_path):
    slept: list[float] = []
    handler = Recorder(robots="User-agent: *\nCrawl-delay: 10\nAllow: /\n")
    with make_fetcher(tmp_path, handler, sleeper=slept.append) as fetcher:
        fetcher.get(PAGE_URL)
        fetcher.get(OTHER_URL)
    assert max(slept) > 5.0


# --------------------------------------------------------------- rate limit, rule 5


def test_rate_limiter_sleeps_between_two_urls_on_the_same_host(tmp_path):
    slept: list[float] = []
    handler = Recorder()
    with make_fetcher(tmp_path, handler, sleeper=slept.append) as fetcher:
        fetcher.get(PAGE_URL)
        before = len(slept)
        fetcher.get(OTHER_URL)

    assert handler.calls == [ROBOTS_URL, PAGE_URL, OTHER_URL]
    # The second page is a different URL on the same host, so it must have waited.
    assert len(slept) > before
    assert all(0 < wait <= 1.0 for wait in slept)


def test_cache_hit_does_not_sleep(tmp_path):
    slept: list[float] = []
    handler = Recorder()
    with make_fetcher(tmp_path, handler) as fetcher:
        fetcher.get(PAGE_URL)
    with make_fetcher(tmp_path, ExplodingHandler(), sleeper=slept.append) as rerun:
        rerun.get(PAGE_URL)
    assert slept == []


# ------------------------------------------------------------------------- failures


def test_server_error_is_retried_then_gives_up(tmp_path):
    handler = Recorder()
    handler.page_status = 503
    with make_fetcher(tmp_path, handler, sleeper=lambda _: None, max_retries=2) as fetcher:
        with pytest.raises(FetchFailed):
            fetcher.get(PAGE_URL)
    assert handler.calls.count(PAGE_URL) == 3
    assert not fetcher.is_cached(PAGE_URL)


def test_not_found_is_not_cached(tmp_path):
    handler = Recorder()
    handler.page_status = 404
    with make_fetcher(tmp_path, handler, sleeper=lambda _: None) as fetcher:
        with pytest.raises(FetchFailed):
            fetcher.get(PAGE_URL)
    assert not fetcher.is_cached(PAGE_URL)


# ------------------------------------------------------------- `jg crawl <COURT>` dispatch


@pytest.mark.parametrize("code", ["NSS", "NS", "US", "ÚS", "ESBIRKA"])
def test_run_crawl_knows_every_source(code):
    assert code in crawl_pkg.SOURCES


def test_run_crawl_rejects_an_unknown_source():
    with pytest.raises(SystemExit, match="unknown source"):
        crawl_pkg.run_crawl("TEST-COURT")


@pytest.mark.parametrize("code", ["NS", "US", "ESBIRKA"])
def test_run_crawl_surfaces_the_sources_own_blocker(code):
    """These three sources still refuse. The message must survive to the CLI, not a traceback.

    No network and no database: each ``crawl()`` refuses before it touches the fetcher, so
    this also pins that the refusal stays eager.

    ``NSS`` is deliberately not in this list any more: since ``vyhledavac.nssoud.cz`` was
    allowlisted its ``crawl()`` really crawls, and its behaviour is pinned off-line in
    ``test_nssoud.py`` against the pages that crawl cached.
    """
    with pytest.raises(SystemExit) as excinfo:
        crawl_pkg.run_crawl(code)
    assert "CLAUDE.md" in str(excinfo.value) or "not implemented" in str(excinfo.value)


def test_nss_no_longer_refuses():
    """The counterpart to the list above: NSS must not have quietly regressed to a stub."""
    from jg.crawl import nssoud

    assert not hasattr(nssoud, "CrawlUnavailable")
    assert callable(nssoud.crawl)

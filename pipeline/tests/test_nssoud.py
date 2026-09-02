"""The NSS crawler, driven entirely off pages this project actually fetched.

Every fixture here is read out of the on-disk crawl cache under ``data/raw/NSS/`` — the
search form, one day's result page, one paging fragment, one decision — and located by
recomputing the same cache key the :class:`~jg.crawl.base.Fetcher` used. Nothing in this
file may touch the network; the only ``httpx`` client that appears is one whose transport
raises on any request at all, used to prove that a second crawl over a crawled window is
served entirely from disk.

The cache is gitignored, so the fixtures skip rather than fail in a checkout that has not
crawled yet. The message says which command fills them in.

Window on disk when these assertions were written: 1.–5. 1. 2024 (65 records, 27 of them
NSS, fully document-fetched) plus 30. 1. 2024 listed but not document-fetched, because it
is the day that forces the paging path.
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import re

import httpx
import pytest

from jg.crawl import nssoud
from jg.crawl.base import Fetcher
from jg.extract.patterns import find_references, pattern_set
from jg.extract.resolver import alias_candidates, normalize_alias
from jg.models import PanelType, ReferenceKind

# --------------------------------------------------------------------------- fixtures

#: A day inside the crawled window whose 16 records fit on the first result page, so its
#: listing is exact in one request.
CRAWLED_DAY = dt.date(2024, 1, 3)

#: The day that needs paging: 58 records, more than the 40 the first page holds.
PAGED_DAY = dt.date(2024, 1, 30)

#: A decision from the crawled window. Both identifiers were read off the result page.
SAMPLE_DOCUMENT_ID = "717424"
SAMPLE_ECLI = "ECLI:CZ:NSS:2024:6.Afs.234.2023.50"
SAMPLE_REF_NO_AS_PRINTED = "6 Afs 234/2023 - 50"

_SKIP = (
    "no NSS crawl cache under data/raw/NSS. Run a crawl over 1.–5. 1. 2024 first; "
    "these tests read the pages it saves and assert on their real contents."
)


@pytest.fixture(scope="module")
def fetcher() -> Fetcher:
    """A Fetcher used only for its cache-path arithmetic. It issues no requests."""
    return Fetcher(nssoud.COURT_CODE)


def _cached(path) -> str:
    if not path.exists():
        pytest.skip(_SKIP)
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def form_html(fetcher: Fetcher) -> str:
    return _cached(fetcher.cache_paths(nssoud.FORM_URL)[0])


@pytest.fixture(scope="module")
def form_fields(form_html: str) -> dict[str, str]:
    return nssoud.parse_search_form(form_html)


def _result_html(fetcher: Fetcher, form_fields: dict[str, str], day: dt.date) -> str:
    payload = nssoud.search_payload(form_fields, day, day)
    body, _sidecar = fetcher.post_cache_paths(
        nssoud.SEARCH_URL, payload, cache_omit=nssoud.VOLATILE_FIELDS
    )
    return _cached(body)


@pytest.fixture(scope="module")
def result_html(fetcher: Fetcher, form_fields: dict[str, str]) -> str:
    return _result_html(fetcher, form_fields, CRAWLED_DAY)


@pytest.fixture(scope="module")
def fragment_html(fetcher: Fetcher, form_fields: dict[str, str]) -> str:
    """Page 1 of the paged day, as ``/Home/MyResTRowsCont`` returned it."""
    page = _result_html(fetcher, form_fields, PAGED_DAY)
    state = nssoud.parse_scroll_state(page)
    assert state is not None, "the paged day's result page carries no scroll state"
    body, _sidecar = fetcher.post_cache_paths(
        nssoud.BASE_URL.rstrip("/") + state.more_rows_url,
        nssoud.page_payload(state, 1),
    )
    return _cached(body)


@pytest.fixture(scope="module")
def document_html(fetcher: Fetcher) -> str:
    url = nssoud.DOCUMENT_URL_TEMPLATE.format(document_id=SAMPLE_DOCUMENT_ID)
    return _cached(fetcher.cache_paths(url)[0])


@pytest.fixture(scope="module")
def sample_row(result_html: str) -> nssoud.ResultRow:
    rows = {row.ecli: row for row in nssoud.parse_result_rows(result_html)}
    if SAMPLE_ECLI not in rows:
        pytest.skip(f"{SAMPLE_ECLI} is not on the cached result page for {CRAWLED_DAY}")
    return rows[SAMPLE_ECLI]


# ------------------------------------------------------------------------ the form


def test_search_form_carries_the_date_range_condition(form_fields):
    """The two fields the whole search hangs on, by their exact live names."""
    assert nssoud.DATE_FROM_FIELD in form_fields
    assert nssoud.DATE_TO_FIELD in form_fields
    # The rendered form is one <form> with ~299 named inputs; posting a subset drops
    # hidden condition metadata and the server answers with an unfiltered search.
    assert len(form_fields) > 250


def test_search_payload_writes_czech_dates_and_the_submit_marker(form_fields):
    payload = nssoud.search_payload(form_fields, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert payload[nssoud.DATE_FROM_FIELD] == "1.1.2024"
    assert payload[nssoud.DATE_TO_FIELD] == "31.1.2024"
    assert payload[nssoud.SUBMIT_FIELD] == ""
    # Every other field is passed through untouched.
    assert len(payload) >= len(form_fields)


# --------------------------------------------------------------------- result rows


def test_result_table_columns_are_in_the_documented_order(result_html):
    """Pins the column contract the ``zobrazeniVysledkuVolba=5`` view produces.

    Every field this crawler reads is taken by position, so a column inserted upstream
    would quietly shift the ECLI into the participants' names. This is the assertion that
    turns that into a failing test.
    """
    from selectolax.parser import HTMLParser

    table = HTMLParser(result_html).css_first(nssoud.RESULTS_TABLE_SELECTOR)
    headers = [th.text(strip=True) for th in table.css("th")]
    assert headers == [
        "",
        "#",
        "Datum",
        "Číslo jednací",
        "Soud (senát)",
        "Druh dokumentu",
        "Výrok rozhodnutí NSS",
        "Účastníci řízení",
        "ECLI",
        "Kasační/ústavní stížnost",
        "Možnosti",
    ]


def test_parse_total_count_reads_the_reported_total(result_html):
    assert nssoud.parse_total_count(result_html) == 16


def test_parse_total_count_refuses_a_page_without_the_phrase():
    with pytest.raises(ValueError, match="Počet nalezených záznamů"):
        nssoud.parse_total_count("<html><body>nic</body></html>")


def test_parse_result_rows_reads_the_row_off_the_page(sample_row):
    assert sample_row.ecli == SAMPLE_ECLI
    assert sample_row.decided_on == CRAWLED_DAY
    assert sample_row.case_no == "6 Afs 234/2023"
    assert sample_row.ref_no == "6 Afs 234/2023-50"
    assert sample_row.court_panel == "tříčlenný senát NSS"
    assert sample_row.document_kind == "Rozsudek"
    assert sample_row.outcome == "zamítnuto"
    assert sample_row.document_id == SAMPLE_DOCUMENT_ID
    assert sample_row.is_nss


def test_every_parsed_row_carries_a_published_ecli(result_html):
    rows = nssoud.parse_result_rows(result_html)
    assert len(rows) == 16
    assert all(nssoud._ECLI_RE.match(row.ecli) for row in rows)
    assert all(row.document_id.isdigit() for row in rows)


def test_the_result_set_is_wider_than_nss(result_html):
    """The same database serves the regional administrative courts.

    ``decision.court_code`` is a foreign key onto the three seeded courts, so those rows
    are counted and skipped rather than loaded. If this ever stops holding, the filter in
    :func:`jg.crawl.nssoud.crawl` has become dead code and the report is overstating
    coverage.
    """
    rows = nssoud.parse_result_rows(result_html)
    others = [row for row in rows if not row.is_nss]
    assert others, "expected regional-court rows in an administrative-courts search"
    assert all(row.ecli.startswith("ECLI:CZ:") for row in others)
    assert not any(row.ecli.startswith("ECLI:CZ:NSS:") for row in others)


def test_document_id_is_taken_from_the_row_links(result_html):
    """All three per-row links carry the same numeric key; any of them identifies it."""
    hits = nssoud._DOCUMENT_ID_RE.findall(result_html)
    assert SAMPLE_DOCUMENT_ID in hits
    # /Index/, /Text/ and /Html/ for each of the 16 rows, plus the detail link.
    assert hits.count(SAMPLE_DOCUMENT_ID) >= 3


# ------------------------------------------------------------------------- paging


def test_parse_scroll_state_unescapes_the_inline_javascript(result_html):
    state = nssoud.parse_scroll_state(result_html)
    assert state is not None
    assert state.more_rows_url == "/Home/MyResTRowsCont"
    assert state.view_id == "5"
    # The page writes every quote as "; posting the escaped literal returns no rows.
    assert state.params.startswith('[{"Id"')
    assert '"TechnickyNazev":"datumvydanirozhodnuti"' in state.params
    assert "2024-01-03T00:00:00" in state.params
    assert "order by" in state.sort


def test_page_payload_uses_the_field_names_the_endpoint_expects(result_html):
    state = nssoud.parse_scroll_state(result_html)
    payload = nssoud.page_payload(state, 3)
    assert set(payload) == {
        "vyhledavaciPodminky",
        "zobrazeniVysledkuId",
        "pageNum",
        "resultOrder",
    }
    assert payload["pageNum"] == "3"


def test_paging_fragment_parses_like_a_result_page(fragment_html):
    """The fragment is a bare ``<tbody>``; an HTML parser drops it unless it is wrapped."""
    rows = nssoud.parse_result_rows(fragment_html)
    assert rows, "no rows parsed out of the paging fragment"
    assert all(nssoud._ECLI_RE.match(row.ecli) for row in rows)
    assert all(row.decided_on == PAGED_DAY for row in rows)


def test_an_empty_fragment_is_the_end_of_the_results():
    assert nssoud.parse_result_rows("") == []
    assert nssoud.parse_result_rows("\n  \n") == []


# ------------------------------------------------------------------- panel detection


def _panel_vocabulary(form_html: str) -> list[str]:
    """The court bodies the site's own "Soud (senát)" dial tree names.

    Read out of the cached form page rather than typed here, so the test is pinned to what
    the site says rather than to what this module hopes it says.
    """
    unescaped = html_module.unescape(form_html)
    start = unescaped.find("ciselnikTreeData")
    end = unescaped.find("/>", start)
    return re.findall(r'title:"([^"]+)"', unescaped[start:end])


def test_the_sites_panel_vocabulary_is_what_this_module_expects(form_html):
    vocabulary = _panel_vocabulary(form_html)
    for expected in (
        "tříčlenný senát NSS",
        "rozšířený senát NSS",
        "7členný RS NSS",
        "9členný RS NSS",
        "plénum NSS",
    ):
        assert expected in vocabulary, f"{expected!r} vanished from the dial tree"


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("tříčlenný senát NSS", PanelType.PANEL),
        ("rozšířený senát NSS", PanelType.EXTENDED),
        # The seven- and nine-judge formations of the rozšířený senát, in the abbreviated
        # spelling the site uses. Reading these as an ordinary panel would delete exactly
        # the departure signal M3 is built on.
        ("7členný RS NSS", PanelType.EXTENDED),
        ("9členný RS NSS", PanelType.EXTENDED),
        ("plénum NSS", PanelType.PLENARY),
        ("kompetenční senát NSS", PanelType.PANEL),
        ("volební senát NSS", PanelType.PANEL),
        ("kárný senát", PanelType.PANEL),
    ],
)
def test_panel_type_maps_the_court_column(column, expected):
    assert nssoud.panel_type(court_panel=column) is expected


def test_panel_type_is_unknown_only_with_nothing_to_read():
    assert nssoud.panel_type() is PanelType.UNKNOWN
    assert nssoud.panel_type(court_panel="   ") is PanelType.UNKNOWN


def test_every_crawled_row_yields_a_panel_type(result_html):
    for row in nssoud.parse_result_rows(result_html):
        assert nssoud.panel_type(court_panel=row.court_panel) is not PanelType.UNKNOWN


# ------------------------------------------------------------------ decision parsing


def test_document_paragraphs_line_up_with_the_courts_own_numbering(document_html):
    document = nssoud.parse_document(document_html)
    assert document.numbered
    for index, body in enumerate(document.paragraphs, start=1):
        if body:
            assert body.startswith(f"[{index}]"), body[:40]
    # Position 25 is the decision's paragraph [25], which is what a reader looks up.
    assert document.paragraphs[24].startswith("[25] Na závěr")


def test_document_verdict_is_the_passage_after_takto(document_html):
    document = nssoud.parse_document(document_html)
    assert document.verdict_text is not None
    lines = document.verdict_text.splitlines()
    assert lines[0] == "Kasační stížnost žalobkyně se zamítá."
    # The reasoning must not bleed into the výrok: only that block feeds structural QUASHED.
    assert "Odůvodnění" not in document.verdict_text
    assert not any(line.startswith("[1]") for line in lines)


def test_document_date_agrees_with_the_result_row(document_html, sample_row):
    """Two independent readings of the same fact. They must not disagree."""
    assert nssoud.parse_document(document_html).decided_on == sample_row.decided_on


def test_align_paragraphs_falls_back_to_sequential_when_nothing_is_numbered():
    plain = ["První odstavec.", "Druhý odstavec.", "Třetí odstavec."]
    aligned, numbered = nssoud.align_paragraphs(plain)
    assert numbered is False
    assert aligned == plain


def test_a_single_bracketed_number_is_not_a_numbering_scheme():
    """One "[1]" quoted from another decision must not be read as this one's numbering."""
    quoted = ["Soud odkázal na bod", "[1] citovaného rozsudku", "a pokračoval."]
    aligned, numbered = nssoud.align_paragraphs(quoted)
    assert numbered is False
    assert aligned == quoted


def test_parse_decision_takes_the_ecli_from_the_row_not_the_document(
    document_html, sample_row
):
    """CLAUDE.md rule 1. The decision page never prints its own ECLI; the result view does."""
    assert SAMPLE_ECLI not in document_html
    fetched_at = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)
    decision = nssoud.parse_decision(
        document_html,
        row=sample_row,
        source_url=sample_row.document_url,
        fetched_at=fetched_at,
    )
    assert decision.ecli == SAMPLE_ECLI
    assert decision.court_code == "NSS"
    assert decision.panel_type is PanelType.PANEL
    assert decision.decided_on == CRAWLED_DAY
    assert decision.case_no == "6 Afs 234/2023"
    assert decision.ref_no == "6 Afs 234/2023-50"
    # No R-číslo is published on either page, so none is invented.
    assert decision.journal_no is None
    assert decision.source_url.endswith(f"/DokumentOriginal/Html/{SAMPLE_DOCUMENT_ID}")
    assert decision.verdict_text
    assert len(decision.paragraphs) > 20


# ------------------------------------------------------------------------- aliases


def test_split_ref_no_separates_the_case_number_from_the_sheet():
    case_no, ref_no = nssoud.split_ref_no(SAMPLE_REF_NO_AS_PRINTED)
    assert case_no == "6 Afs 234/2023"
    assert ref_no == "6 Afs 234/2023-50"


def test_split_ref_no_leaves_a_value_without_a_sheet_alone():
    # "Na 230/2023" is the registry mark of a decision in the crawled window.
    assert nssoud.split_ref_no("Na 230/2023") == ("Na 230/2023", "Na 230/2023")


def test_the_stored_ref_no_is_the_string_the_resolver_looks_for():
    """The crawler's alias and the resolver's lookup key must be the same string.

    ``č. j. 10 As 346/2021-38`` is a real citation, taken from paragraph 8 of
    ``ECLI:CZ:NSS:2024:2.As.331.2023.19`` in the crawled corpus. The site prints the same
    number in its result column with spaces around the dash, so if the crawler stored that
    spelling the alias could never be matched and every such citation would count as "not
    in corpus" — a coverage figure that was wrong rather than honest.
    """
    citation = "Krajský soud odkázal na rozsudek č. j. 10 As 346/2021-38."
    references = find_references(citation, 8, pattern_set())
    ref_nos = [r for r in references if r.kind is ReferenceKind.REF_NO]
    assert ref_nos, "the ref_no pattern no longer matches a real crawled citation"

    _case_no, stored = nssoud.split_ref_no("10 As 346/2021 - 38")
    assert normalize_alias(stored) == alias_candidates(ref_nos[0])[0]


def test_the_stored_case_number_is_also_a_resolver_candidate():
    """A document that cites the spisová značka alone must resolve too."""
    references = find_references("sp. zn. 6 Afs 234/2023", 1, pattern_set())
    case_refs = [r for r in references if r.kind is ReferenceKind.CASE_NO]
    assert case_refs
    case_no, _ref_no = nssoud.split_ref_no(SAMPLE_REF_NO_AS_PRINTED)
    assert normalize_alias(case_no) in alias_candidates(case_refs[0])


# ------------------------------------------------------- the whole crawl, off-line


class NoNetwork(httpx.BaseTransport):
    """Any request at all is a test failure."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"network request issued for {request.url}")


def test_recrawling_a_crawled_window_issues_no_requests_at_all():
    """CLAUDE.md rule 5, end to end, and the property the bulk load depends on.

    The whole crawl — the form, five searches, twenty-seven decision pages — replays from
    ``data/raw/NSS/``. The transport underneath raises on contact, so the only way this
    passes is if nothing reached for the network.
    """
    client = httpx.Client(transport=NoNetwork())
    report = nssoud.CrawlReport()
    with Fetcher(nssoud.COURT_CODE, client=client) as offline:
        if not offline.is_cached(nssoud.FORM_URL):
            pytest.skip(_SKIP)
        decisions = list(
            nssoud.crawl(
                offline,
                since=dt.date(2024, 1, 1),
                until=dt.date(2024, 1, 5),
                report=report,
            )
        )

    assert report.days == 5
    assert report.reported_total == 65
    assert report.rows_seen == 65, "the cached listing lost rows on replay"
    assert report.short_days == {}
    assert report.nss_rows == 27
    assert report.other_court_rows == 38
    assert len(decisions) == 27
    assert {d.court_code for d in decisions} == {"NSS"}
    assert all(d.ecli.startswith("ECLI:CZ:NSS:2024:") for d in decisions)
    assert all(d.paragraphs for d in decisions)
    assert report.unnumbered == 0


def test_limit_stops_the_iterator_without_touching_the_network():
    client = httpx.Client(transport=NoNetwork())
    with Fetcher(nssoud.COURT_CODE, client=client) as offline:
        if not offline.is_cached(nssoud.FORM_URL):
            pytest.skip(_SKIP)
        decisions = list(
            nssoud.crawl(
                offline, limit=3, since=dt.date(2024, 1, 1), until=dt.date(2024, 1, 5)
            )
        )
    assert len(decisions) == 3


def test_an_inverted_window_is_refused():
    client = httpx.Client(transport=NoNetwork())
    with Fetcher(nssoud.COURT_CODE, client=client) as offline:
        with pytest.raises(ValueError, match="empty window"):
            list(
                nssoud.crawl(
                    offline, since=dt.date(2024, 1, 5), until=dt.date(2024, 1, 1)
                )
            )

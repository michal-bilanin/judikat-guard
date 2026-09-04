"""The NALUS (ÚS) parser and the ÚS document-key convention.

Every fixture is a page this project actually fetched, read out of ``data/raw/US/`` and
located by recomputing the same cache key the :class:`~jg.crawl.base.Fetcher` used. Nothing
here touches the network: the one ``httpx`` client that appears has a transport that raises
on any request, which is how "a re-run issues zero network requests" (CLAUDE.md rule 5) is
proven rather than asserted.

The cache is gitignored, so a checkout that has not loaded any ÚS decisions skips these
rather than failing. ``make load-us`` fills it in.

Every case number, date and SbNU citation below was read off one of those pages. Nothing is
invented (CLAUDE.md rule 1).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from jg.classify.structural import CitationEdge, classify_structural, marker_set
from jg.crawl import nalus
from jg.crawl.base import Fetcher
from jg.extract.patterns import find_references
from jg.extract.resolver import alias_candidates, normalize_alias
from jg.models import PanelType, ReferenceKind, TreatmentLabel
from jg.normalize import alias_rows

# ------------------------------------------------------------------ fixtures on disk

#: The known-good structural QUASHED case: an ÚS nález whose výrok annuls a named NSS
#: judgment. PLAN.md section 18 records it; the page is cached under data/raw/US.
QUASHING_KEY = "4-3523-20_1"
QUASHING_CASE_NO = "IV. ÚS 3523/20"

#: The NSS decision that výrok annuls, exactly as the výrok names it.
ANNULLED_REF_NO = "5 Afs 470/2019-33"

#: A plenary nález, promulgated in the Sbírka zákonů as well as reported in SbNU.
PLENARY_KEY = "Pl-44-21_1"

#: A usnesení: no SbNU citation, and a výrok that *rejects* rather than annuls. The common
#: case, and the one a naive quashing rule would get wrong.
REJECTION_KEY = "2-875-20_1"

#: An older, SbNU-typeset nález that announces its výrok with neither "takto:" nor a
#: "Výrok" heading, so only the markup path finds it.
UNMARKED_VERDICT_KEY = "1-741-06_1"

#: A key NALUS answers with HTTP 200 and an *empty* document: every header label blank and
#: an empty td.DocContent, though lblDecisionForm still reads USNESENÍ. Cited 499 times in
#: the corpus and still unreachable — a coverage fact, reported rather than papered over.
EMPTY_DOCUMENT_KEY = "3-84-94_1"

_SKIP = (
    "no NALUS cache under data/raw/US. Run `make load-us LIMIT=40` first; these tests read "
    "the pages it saves and assert on their real contents."
)


@pytest.fixture(scope="module")
def fetcher() -> Fetcher:
    """A Fetcher used only for its cache-path arithmetic. It issues no requests."""
    return Fetcher(nalus.COURT_CODE)


def cached(fetcher: Fetcher, document_key: str) -> str:
    path, _sidecar = fetcher.cache_paths(nalus.text_url(document_key))
    if not path.exists():
        pytest.skip(_SKIP)
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def quashing_html(fetcher: Fetcher) -> str:
    return cached(fetcher, QUASHING_KEY)


@pytest.fixture(scope="module")
def plenary_html(fetcher: Fetcher) -> str:
    return cached(fetcher, PLENARY_KEY)


@pytest.fixture(scope="module")
def rejection_html(fetcher: Fetcher) -> str:
    return cached(fetcher, REJECTION_KEY)


# ------------------------------------------------------- the case number -> key mapping


@pytest.mark.parametrize(
    ("case_no", "document_key"),
    [
        # All five were fetched live and returned HTTP 200 with the full decision text.
        ("II. ÚS 2379/08", "2-2379-08_1"),
        ("III. ÚS 989/08", "3-989-08_1"),
        ("I. ÚS 741/06", "1-741-06_1"),
        ("Pl. ÚS 44/21", "Pl-44-21_1"),
        ("IV. ÚS 3523/20", "4-3523-20_1"),
    ],
)
def test_nalus_sz_matches_the_live_document_key(case_no, document_key):
    assert nalus.nalus_sz(case_no) == document_key
    assert nalus.text_url(document_key).endswith(f"GetText.aspx?sz={document_key}")


def test_the_key_is_insensitive_to_the_space_after_the_numeral():
    """NALUS prints "IV.ÚS 3523/20"; citing decisions write "IV. ÚS 3523/20"."""
    assert nalus.nalus_sz("IV.ÚS 3523/20") == nalus.nalus_sz("IV. ÚS 3523/20")


def test_a_four_digit_filing_year_folds_to_the_two_digit_key():
    """The corpus cites the same plenary nález both ways; NALUS answers only the short key."""
    assert nalus.nalus_sz("Pl. ÚS 44/2021") == nalus.nalus_sz("Pl. ÚS 44/21") == "Pl-44-21_1"


def test_nalus_sz_declines_anything_that_is_not_a_us_case_number():
    for value in ("5 Afs 470/2019", "ÚS 1/09", "", "N 144/107 SbNU 211"):
        assert nalus.nalus_sz(value) is None


def test_registry_sign_to_sz_still_yields_the_stem():
    assert nalus.registry_sign_to_sz("II. ÚS 2379/08") == "2-2379-08"
    assert nalus.registry_sign_to_sz("Pl. ÚS 44/21") == "Pl-44-21"
    with pytest.raises(ValueError, match="registry sign"):
        nalus.registry_sign_to_sz("5 Afs 470/2019")


def test_the_primary_key_is_the_namespaced_document_key():
    """CLAUDE.md rule 1: NALUS publishes no ECLI, so none is constructed. V5 says the same."""
    assert nalus.namespaced_ecli(QUASHING_KEY) == "nalus:4-3523-20_1"
    assert not nalus.namespaced_ecli(QUASHING_KEY).startswith("ECLI")
    assert nalus.key_from_url(nalus.text_url(QUASHING_KEY)) == QUASHING_KEY
    assert nalus.key_from_url("https://nalus.usoud.cz/Search/Search.aspx") is None


def test_matches_sign_is_what_makes_the_year_fold_safe():
    assert nalus.matches_sign("Pl. ÚS 44/2021", "Pl.ÚS 44/21")
    assert nalus.matches_sign("IV. ÚS 3523/20", "IV.ÚS 3523/20")
    assert not nalus.matches_sign("IV. ÚS 3523/20", "IV.ÚS 3524/20")
    assert not nalus.matches_sign("IV. ÚS 3523/20", None)


def test_alias_spellings_covers_both_forms_seen_in_the_wild():
    assert nalus.alias_spellings("IV.ÚS 3523/20") == ["IV. ÚS 3523/20", "IV.ÚS 3523/20"]
    assert nalus.alias_spellings("Pl.ÚS 44/21") == ["Pl. ÚS 44/21", "Pl.ÚS 44/21"]
    assert nalus.alias_spellings("5 Afs 470/2019") == []


# ------------------------------------------------------------------------ the header


def test_parse_header_reads_the_real_header_line(quashing_html: str):
    case_no, decided_on, journal_no, title = nalus.parse_header(quashing_html)
    assert case_no == "IV.ÚS 3523/20"
    assert decided_on == dt.date(2021, 8, 24)
    assert journal_no == "N 144/107 SbNU 211"
    assert title and "nečinnosti" in title


def test_journal_no_is_null_when_the_decision_was_not_reported(rejection_html: str):
    """Unpublished is the normal state, not a parse failure."""
    header = nalus.parse_header(rejection_html)
    assert header.case_no == "II.ÚS 875/20"
    assert header.journal_no is None


def test_journal_no_takes_the_sbnu_citation_not_the_sbirka_number(plenary_html: str):
    """Pl. ÚS 44/21 prints "38/2023 Sb." and "N 11/116 SbNU 87" in the same span.

    The first is an act number; putting it in ``journal_no`` would store an act number in a
    decision column.
    """
    document = nalus.parse_gettext(plenary_html)
    assert document.parallel_citations == ["38/2023 Sb.", "N 11/116 SbNU 87"]
    assert document.parallel_citation == "N 11/116 SbNU 87"


# -------------------------------------------------------------------- the panel type


def test_the_plenum_is_detected_from_its_registry_sign(plenary_html: str):
    assert nalus.parse_panel_type(plenary_html) is PanelType.PLENARY


def test_a_numbered_panel_is_a_panel(quashing_html: str, rejection_html: str):
    assert nalus.parse_panel_type(quashing_html) is PanelType.PANEL
    assert nalus.parse_panel_type(rejection_html) is PanelType.PANEL


# ------------------------------------------------------------------------ the výrok


def test_parse_verdict_carries_the_annulment_verbatim(quashing_html: str):
    """The whole point: RawDecision.verdict_text is the only input to the QUASHED rule."""
    verdict = nalus.parse_verdict(quashing_html)
    assert verdict is not None
    assert ANNULLED_REF_NO in verdict
    assert verdict.rstrip().endswith("se ruší.")
    assert "Odůvodnění" not in verdict


def test_a_rejection_is_not_an_annulment(rejection_html: str):
    """Most constitutional complaints are rejected. A false QUASHED is D8's one red line."""
    assert nalus.parse_verdict(rejection_html) == "Ústavní stížnost se odmítá."


def test_the_verdict_is_found_without_a_takto_or_a_vyrok_heading(fetcher: Fetcher):
    """I. ÚS 741/06 announces its operative part with nothing at all; the markup does."""
    verdict = nalus.parse_verdict(cached(fetcher, UNMARKED_VERDICT_KEY))
    assert verdict is not None
    assert verdict.rstrip().endswith("Toto rozhodnutí se zrušuje.")
    assert "Odůvodnění" not in verdict


def test_the_marker_path_agrees_with_the_markup_path(quashing_html: str):
    """Two independent readings of the same operative part must not diverge."""
    paragraphs = nalus.parse_paragraphs(quashing_html)
    assert nalus.verdict_from_paragraphs(paragraphs) == nalus.bold_verdict(
        quashing_html, paragraphs
    )


def test_a_takto_deep_in_the_reasoning_is_not_this_courts_verdict():
    """A quoted lower-court operative part must not become the citing court's own."""
    paragraphs = ["Nález", "Ústavní soud rozhodl takto:", "Rozsudek se ruší.", "Odůvodnění"]
    paragraphs += [f"{n}. …" for n in range(1, 10)]
    paragraphs += ["Krajský soud rozhodl takto:", "Žaloba se zamítá."]
    assert nalus.verdict_from_paragraphs(paragraphs) == "Rozsudek se ruší."


# ------------------------------------------------------------------- the whole page


def test_parse_decision_builds_a_raw_decision_off_the_page(quashing_html: str):
    raw = nalus.parse_decision(
        quashing_html,
        source_url=nalus.text_url(QUASHING_KEY),
        fetched_at=dt.datetime(2026, 9, 4, tzinfo=dt.UTC),
    )
    assert raw.ecli == "nalus:4-3523-20_1"
    assert raw.court_code == "US"
    assert raw.panel_type is PanelType.PANEL
    assert raw.decided_on == dt.date(2021, 8, 24)
    assert raw.case_no == "IV.ÚS 3523/20"
    assert raw.journal_no == "N 144/107 SbNU 211"
    assert raw.verdict_text and ANNULLED_REF_NO in raw.verdict_text
    assert len(raw.paragraphs) > 50


def test_parse_decision_refuses_a_url_that_carries_no_document_key(quashing_html: str):
    with pytest.raises(ValueError, match="document key"):
        nalus.parse_decision(
            quashing_html,
            source_url="https://nalus.usoud.cz/Search/Search.aspx",
            fetched_at=dt.datetime(2026, 9, 4, tzinfo=dt.UTC),
        )


def test_the_annulled_reference_is_extractable_from_the_stored_paragraphs(quashing_html: str):
    """The výrok reaches ``citation`` through the ordinary extraction pass, not a shortcut."""
    raw = nalus.parse_decision(
        quashing_html,
        source_url=nalus.text_url(QUASHING_KEY),
        fetched_at=dt.datetime(2026, 9, 4, tzinfo=dt.UTC),
    )
    found = [
        ref
        for idx, body in enumerate(raw.paragraphs, start=1)
        for ref in find_references(body, idx)
        if ref.kind is ReferenceKind.REF_NO and ref.groups.get("ref_no") == "5 Afs 470/2019"
    ]
    assert found, "the výrok's č. j. must be extractable"
    assert normalize_alias(ANNULLED_REF_NO) in alias_candidates(found[0])


# ---------------------------------------------------------------------------- aliases


def test_both_case_number_spellings_become_aliases(quashing_html: str):
    """What makes the corpus's ÚS references resolve. Uses the resolver's own normaliser."""
    raw = nalus.parse_decision(
        quashing_html,
        source_url=nalus.text_url(QUASHING_KEY),
        fetched_at=dt.datetime(2026, 9, 4, tzinfo=dt.UTC),
    )
    loader = _load_loader()
    target = loader.Target(
        document_key=QUASHING_KEY,
        spellings=[QUASHING_CASE_NO, "IV. ÚS 3523/2020"],
        mentions=2,
    )
    aliases = {alias for alias, _kind, _ecli in alias_rows(raw)}
    aliases |= {alias for alias, _kind, _ecli in loader.extra_aliases(raw, target)}

    assert normalize_alias("IV. ÚS 3523/20") in aliases
    assert normalize_alias("IV.ÚS 3523/20") in aliases
    assert normalize_alias("IV. ÚS 3523/2020") in aliases
    assert normalize_alias("N 144/107 SbNU 211") in aliases
    assert all(ecli == raw.ecli for _a, _k, ecli in loader.extra_aliases(raw, target))


def test_extra_aliases_refuse_a_spelling_the_page_does_not_confirm(quashing_html: str):
    raw = nalus.parse_decision(
        quashing_html,
        source_url=nalus.text_url(QUASHING_KEY),
        fetched_at=dt.datetime(2026, 9, 4, tzinfo=dt.UTC),
    )
    loader = _load_loader()
    target = loader.Target(QUASHING_KEY, [QUASHING_CASE_NO, "IV. ÚS 9999/20"], 2)
    aliases = {alias for alias, _kind, _ecli in loader.extra_aliases(raw, target)}
    assert normalize_alias("IV. ÚS 9999/20") not in aliases


def _load_loader():
    """Import ``scripts/load_us_citations.py``, which is a script rather than a package."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "load_us_citations.py"
    spec = importlib.util.spec_from_file_location("load_us_citations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("load_us_citations", module)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------- the structural QUASHED rule


def test_the_us_vyrok_produces_a_structural_quashed(quashing_html: str):
    """The red light, end to end and off-line: PLAN.md section 8, tier 1, no model call.

    The aliases are the ones the NSS crawler stores for the annulled judgment, in the form
    ``jg.extract.resolver.normalize_alias`` writes them.
    """
    raw = nalus.parse_decision(
        quashing_html,
        source_url=nalus.text_url(QUASHING_KEY),
        fetched_at=dt.datetime(2026, 9, 4, tzinfo=dt.UTC),
    )
    edge = CitationEdge(
        citation_id=1,
        citing_ecli=raw.ecli,
        citing_court="US",
        citing_panel=raw.panel_type,
        citing_date=raw.decided_on,
        cited_ecli="ECLI:CZ:NSS:2020:5.Afs.470.2019.33",
        cited_court="NSS",
        cited_aliases=(
            normalize_alias("5 Afs 470/2019"),
            normalize_alias(ANNULLED_REF_NO),
        ),
        raw_text="č. j. 5 Afs 470/2019-33",
        verdict_text=raw.verdict_text,
    )
    result = classify_structural(edge, marker_set())
    assert result is not None
    assert result.label is TreatmentLabel.QUASHED
    assert result.confidence == 1.0
    assert ANNULLED_REF_NO in result.evidence_span
    assert result.evidence_span in " ".join((raw.verdict_text or "").split())


def test_a_rejection_vyrok_produces_no_quashed(rejection_html: str):
    """Guard against the manufactured red light: "se odmítá" annuls nothing."""
    verdict = nalus.parse_verdict(rejection_html)
    edge = CitationEdge(
        citation_id=2,
        citing_ecli=nalus.namespaced_ecli(REJECTION_KEY),
        citing_court="US",
        cited_ecli="ECLI:CZ:NSS:2020:5.Afs.470.2019.33",
        cited_aliases=(normalize_alias(ANNULLED_REF_NO),),
        raw_text="č. j. 5 Afs 470/2019-33",
        verdict_text=verdict,
    )
    assert classify_structural(edge, marker_set()) is None


# ------------------------------------------------------------------- rule 5, the cache


class NoNetwork(httpx.BaseTransport):
    """Any request at all is a test failure."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"network request issued for {request.url}")


def test_refetching_a_cached_decision_issues_no_request_at_all(fetcher: Fetcher):
    """CLAUDE.md rule 5. The transport underneath raises on contact."""
    if not fetcher.is_cached(nalus.text_url(QUASHING_KEY)):
        pytest.skip(_SKIP)
    with Fetcher(nalus.COURT_CODE, client=httpx.Client(transport=NoNetwork())) as offline:
        raw = nalus.fetch_decision(offline, QUASHING_CASE_NO)
    assert raw.ecli == nalus.namespaced_ecli(QUASHING_KEY)
    assert raw.verdict_text and ANNULLED_REF_NO in raw.verdict_text


def test_an_empty_document_is_reported_rather_than_stored(fetcher: Fetcher):
    """HTTP 200 with no decision on the page is a coverage fact, not a parse failure.

    Storing it would put a decision row with no date, no case number and no text into the
    corpus and inflate the N the UI quotes; failing loudly would stop a 2,000-key load over
    a gap in the source. It gets its own exception so the run report can count it.
    """
    html = cached(fetcher, EMPTY_DOCUMENT_KEY)
    assert nalus.is_empty_document(html)
    assert nalus.parse_paragraphs(html) == []
    assert nalus.parse_header(html) == nalus.NalusHeader(None, None, None, None)

    with Fetcher(nalus.COURT_CODE, client=httpx.Client(transport=NoNetwork())) as offline:
        with pytest.raises(nalus.EmptyDocument, match="empty document"):
            nalus.fetch_decision(offline, "III. ÚS 84/94")


def test_a_real_page_is_not_mistaken_for_an_empty_one(quashing_html: str):
    assert not nalus.is_empty_document(quashing_html)


def test_fetch_decision_refuses_a_case_number_it_cannot_key(fetcher: Fetcher):
    with Fetcher(nalus.COURT_CODE, client=httpx.Client(transport=NoNetwork())) as offline:
        with pytest.raises(ValueError, match="case number"):
            nalus.fetch_decision(offline, "5 Afs 470/2019")


def test_crawl_still_refuses_and_names_the_route_that_works(fetcher: Fetcher):
    from jg.crawl.base import CrawlUnavailable

    with pytest.raises(CrawlUnavailable, match="load_us_citations"):
        list(nalus.crawl(fetcher))

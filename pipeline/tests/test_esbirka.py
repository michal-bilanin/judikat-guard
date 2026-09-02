"""e-Sbírka: the live API client, dump reading, and above all timeline closure.

PLAN.md section 10, M6. The timeline is where this milestone's correctness lives. The
source hands you start dates; the Java lateral join asks
``valid_from <= d and (valid_to is null or valid_to >= d)`` and assumes at most one row
comes back. Nothing enforces that assumption but :func:`jg.crawl.esbirka.build_timeline`,
so it is tested harder than anything else here.

**No test issues a network request.** The API tests replay responses already cached in
``data/raw/ESBIRKA/`` through a :class:`~jg.crawl.base.Fetcher` whose HTTP client raises on
any call, which is simultaneously the fixture and the proof of CLAUDE.md rule 5. Each one
skips cleanly where the cache is absent, so a fresh checkout still runs green.
"""

from __future__ import annotations

import datetime as dt
import json
from urllib.parse import urlsplit

import httpx
import pytest

from jg import config
from jg.crawl import esbirka
from jg.crawl.base import CrawlUnavailable, Fetcher, ParserSeam

# --- fixtures ---------------------------------------------------------------------
#
# CLAUDE.md rule 1: no invented Czech legal identifiers. The act number below carries the
# `TEST-` prefix so it cannot resolve against the Sbírka, and the wordings are obviously
# synthetic Czech, not quoted statute. The one real act number that appears anywhere in
# these tests is `89/2012`, taken from PLAN.md section 6, and it is used only to pin the
# shape of a key, never as a claim about what that act says.

ACT = "TEST-000/0000"
SECTION = "TEST-1"


def version(
    valid_from: str,
    body: str,
    valid_to: str | None = None,
    *,
    section: str = SECTION,
    subsec: str | None = "1",
    derogated_by: str | None = None,
) -> esbirka.ProvisionVersionRecord:
    return esbirka.ProvisionVersionRecord(
        provision=esbirka.ProvisionRecord(act_no=ACT, section=section, subsec=subsec),
        body=body,
        valid_from=dt.date.fromisoformat(valid_from),
        valid_to=dt.date.fromisoformat(valid_to) if valid_to else None,
        derogated_by=derogated_by,
    )


# --- the observed page ------------------------------------------------------------


def _cached_notice() -> str | None:
    """The body of the ``opendata.eselpoint.cz`` root as actually cached, or None."""
    directory = config.RAW_DIR / esbirka.SOURCE_CODE
    for meta_path in sorted(directory.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - defensive
            continue
        if meta.get("url") == esbirka.BASE_URL:
            body = meta_path.with_suffix("").with_suffix(".html")
            if body.exists():
                return body.read_text(encoding="utf-8")
    return None


def test_is_moved_notice_matches_the_page_actually_cached():
    """Written against markup on disk, not remembered markup."""
    html = _cached_notice()
    if html is None:  # pragma: no cover - only in a checkout with no crawl cache
        pytest.skip(f"no cached {esbirka.BASE_URL} body under {config.RAW_DIR}")
    assert esbirka.is_moved_notice(html)


def test_is_moved_notice_rejects_an_ordinary_page():
    assert not esbirka.is_moved_notice("<html><body>§ 1 Předmět úpravy</body></html>")


def test_crawl_refuses_eagerly_without_a_fetcher():
    """`crawl()` must not need the network to say why it cannot run — nor to say what does.

    There is no bulk crawl for a statute source: the live API is addressed by act, and
    which acts to load follows from the citations already extracted. The refusal therefore
    has to carry the working route, or an operator reads it as "M6 is blocked" when it is
    not.
    """
    with pytest.raises(CrawlUnavailable) as excinfo:
        next(esbirka.crawl(fetcher=None))  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "catch-all" in message
    assert esbirka.SUCCESSOR_URL in message
    assert esbirka.API_BASE_URL in message
    assert "fetch_provision_versions" in message


def test_parse_dump_is_a_declared_seam_not_a_guess():
    """Still a seam. The live API is not a *dump* schema, and nobody has seen that one."""
    with pytest.raises(ParserSeam) as excinfo:
        list(esbirka.parse_dump({"anything": []}))
    assert "invented" in str(excinfo.value)


def test_both_e_sbirka_hosts_are_allowlisted():
    """The successor was authorised on 2026-09-03; the retired host stays so the probe runs.

    CLAUDE.md rule 6 and `config.ALLOWLISTED_HOSTS` must not drift apart, and the API base
    URL must sit on the host that was actually authorised rather than on a sibling nobody
    approved.
    """
    assert "e-sbirka.gov.cz" in config.ALLOWLISTED_HOSTS
    assert "opendata.eselpoint.cz" in config.ALLOWLISTED_HOSTS
    assert urlsplit(esbirka.API_BASE_URL).hostname in config.ALLOWLISTED_HOSTS


# --- normalisation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2000", "2000"),
        ("§ 2000", "2000"),
        ("  § 2000. ", "2000"),
        ("§ 2000a", "2000a"),
    ],
)
def test_normalise_section_strips_the_paragraph_sign(raw, expected):
    """`provision.section` is keyed on the bare number: '§ 2000' would never resolve.

    '2000' is the section PLAN.md section 6 gives as the example for this column.
    """
    assert esbirka.normalise_section(raw) == expected


# --- dump reading -----------------------------------------------------------------


def test_iter_dump_files_finds_only_dumps_and_sorts_them(tmp_path):
    (tmp_path / "b.json").write_text("[]", encoding="utf-8")
    (tmp_path / "a.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.jsonld").write_text("[]", encoding="utf-8")

    found = [path.name for path in esbirka.iter_dump_files(tmp_path)]
    assert found == ["a.jsonl", "b.json", "c.jsonld"]


def test_iter_dump_files_treats_a_missing_directory_as_no_coverage(tmp_path):
    assert list(esbirka.iter_dump_files(tmp_path / "absent")) == []


def test_iter_dump_records_streams_json_lines(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text('{"n": 1}\n\n{"n": 2}\n{"n": 3}\n', encoding="utf-8")
    assert [r["n"] for r in esbirka.iter_dump_records(path)] == [1, 2, 3]


def test_iter_dump_records_bounds_what_it_reads(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text("\n".join(json.dumps({"n": n}) for n in range(100)), encoding="utf-8")
    assert [r["n"] for r in esbirka.iter_dump_records(path, limit=3)] == [0, 1, 2]


def test_iter_dump_records_reports_the_line_a_bad_dump_broke_on(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text('{"n": 1}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"d\.jsonl:2"):
        list(esbirka.iter_dump_records(path))


def test_load_dump_refuses_a_file_too_large_to_slurp(tmp_path, monkeypatch):
    path = tmp_path / "big.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(esbirka, "MAX_DUMP_BYTES", 1)
    with pytest.raises(CrawlUnavailable, match="JSON Lines"):
        esbirka.load_dump(path)


@pytest.mark.parametrize("key", ["records", "items", "data", "@graph"])
def test_a_dump_envelope_may_carry_its_records_under_any_known_key(key, tmp_path):
    payload = {"format": esbirka.LOCAL_FORMAT, key: [_flat("2014-01-01", "první znění")]}
    records = esbirka.parse_local_dump(payload)
    assert [r.valid_from for r in records] == [dt.date(2014, 1, 1)]


def test_an_unrecognised_envelope_raises_rather_than_yielding_nothing():
    with pytest.raises(ValueError, match="no record list"):
        esbirka.parse_local_dump({"format": esbirka.LOCAL_FORMAT, "vysledky": []})


def test_a_foreign_format_declaration_is_refused():
    with pytest.raises(ValueError, match="mapping written"):
        esbirka.parse_local_dump({"format": "esb-otevrena-data.v9", "records": []})


# --- the local record shape -------------------------------------------------------


def _flat(valid_from: str, body: str, **extra: object) -> dict[str, object]:
    record: dict[str, object] = {
        "act_no": ACT,
        "section": SECTION,
        "subsec": "1",
        "body": body,
        "valid_from": valid_from,
    }
    record.update(extra)
    return record


def test_parse_local_dump_reads_the_flat_form():
    [record] = esbirka.parse_local_dump([_flat("2014-01-01", "první znění", valid_to="2016-12-31")])
    assert record.provision.key() == (ACT, SECTION, "1")
    assert record.body == "první znění"
    assert record.valid_from == dt.date(2014, 1, 1)
    assert record.valid_to == dt.date(2016, 12, 31)


def test_a_dump_written_by_this_pipeline_reads_back_unchanged():
    """`model_dump` emits the nested `provision` form; the reader must round-trip it."""
    original = version("2014-01-01", "první znění", "2016-12-31")
    payload = {
        "format": esbirka.LOCAL_FORMAT,
        "records": [json.loads(original.model_dump_json())],
    }
    assert esbirka.parse_local_dump(payload) == [original]


def test_the_key_shape_matches_what_the_resolver_looks_up():
    """`(act_no, section, subsec)` as PLAN.md section 6 prints it: '89/2012', '2000'."""
    [record] = esbirka.parse_local_dump(
        [{"act_no": "89/2012", "section": "§ 2000", "subsec": "odst. 1",
          "body": "x", "valid_from": "2014-01-01"}]
    )
    assert record.provision.key() == ("89/2012", "2000", "1")
    assert record.provision.label() == "§ 2000 odst. 1 zákona č. 89/2012 Sb."


def test_a_record_with_no_body_is_refused():
    with pytest.raises(ValueError, match="missing body"):
        esbirka.parse_local_dump([_flat("2014-01-01", "")])


def test_a_record_with_no_start_date_is_refused():
    record = _flat("2014-01-01", "znění")
    record["valid_from"] = None
    with pytest.raises(ValueError, match="missing valid_from"):
        esbirka.parse_local_dump([record])


def test_a_blank_subsec_is_none_not_empty_string():
    """A citation naming no subsection is a *different* provision row from one that does."""
    [record] = esbirka.parse_local_dump([_flat("2014-01-01", "znění", subsec="")])
    assert record.provision.subsec is None


# --- build_timeline: the invariant ProvisionRepository depends on -----------------


def test_open_windows_are_closed_the_day_before_the_next_one_opens():
    """`valid_to` is inclusive, because VERSION_IN_FORCE asks `valid_to >= :date`."""
    timeline = esbirka.build_timeline(
        [version("2014-01-01", "první"), version("2017-01-01", "druhé")]
    )
    assert [(v.valid_from, v.valid_to) for v in timeline] == [
        (dt.date(2014, 1, 1), dt.date(2016, 12, 31)),
        (dt.date(2017, 1, 1), None),
    ]


def test_exactly_one_version_matches_any_given_date():
    """The property the lateral join assumes and cannot itself check."""
    timeline = esbirka.build_timeline(
        [version("2017-01-01", "druhé"), version("2014-01-01", "první"),
         version("2020-06-01", "třetí")]
    )
    for probe in (
        dt.date(2014, 1, 1), dt.date(2016, 12, 31), dt.date(2017, 1, 1),
        dt.date(2020, 5, 31), dt.date(2020, 6, 1), dt.date(2030, 1, 1),
    ):
        matching = [
            v for v in timeline
            if v.valid_from <= probe and (v.valid_to is None or v.valid_to >= probe)
        ]
        assert len(matching) == 1, f"{len(matching)} versions in force on {probe}"


def test_only_the_last_version_is_left_open():
    timeline = esbirka.build_timeline(
        [version("2014-01-01", "první"), version("2017-01-01", "druhé"),
         version("2020-06-01", "třetí")]
    )
    assert [v.valid_to is None for v in timeline] == [False, False, True]


def test_input_order_does_not_matter():
    forwards = esbirka.build_timeline([version("2014-01-01", "a"), version("2017-01-01", "b")])
    backwards = esbirka.build_timeline([version("2017-01-01", "b"), version("2014-01-01", "a")])
    assert forwards == backwards


def test_a_declared_end_date_that_leaves_a_gap_is_preserved():
    """Repealed in 2016, re-enacted in 2020: there is genuinely no wording in between."""
    timeline = esbirka.build_timeline(
        [version("2014-01-01", "první", "2016-12-31"), version("2020-01-01", "druhé")]
    )
    assert timeline[0].valid_to == dt.date(2016, 12, 31)
    probe = dt.date(2018, 1, 1)
    assert not [
        v for v in timeline
        if v.valid_from <= probe and (v.valid_to is None or v.valid_to >= probe)
    ]


def test_a_declared_end_date_that_overlaps_the_next_version_is_clamped():
    timeline = esbirka.build_timeline(
        [version("2014-01-01", "první", "2018-12-31"), version("2017-01-01", "druhé")]
    )
    assert timeline[0].valid_to == dt.date(2016, 12, 31)


def test_a_republished_identical_wording_does_not_become_a_rewording():
    """The whole amber hangs on `v0.body <> v1.body`; a no-op republication must vanish."""
    timeline = esbirka.build_timeline(
        [version("2014-01-01", "stejné znění"), version("2017-01-01", "stejné znění"),
         version("2020-01-01", "jiné znění")]
    )
    assert [(v.valid_from, v.body) for v in timeline] == [
        (dt.date(2014, 1, 1), "stejné znění"),
        (dt.date(2020, 1, 1), "jiné znění"),
    ]
    assert timeline[0].valid_to == dt.date(2019, 12, 31)


def test_identical_wordings_are_not_coalesced_across_a_derogation():
    timeline = esbirka.build_timeline(
        [version("2014-01-01", "znění"),
         version("2017-01-01", "znění", derogated_by="TEST-ECLI:DEROGACE")]
    )
    assert len(timeline) == 2
    assert timeline[1].derogated_by == "TEST-ECLI:DEROGACE"


def test_two_versions_starting_on_the_same_day_are_refused():
    with pytest.raises(esbirka.TimelineError, match="both start on"):
        esbirka.build_timeline([version("2014-01-01", "a"), version("2014-01-01", "b")])


def test_a_version_ending_before_it_starts_is_refused():
    with pytest.raises(esbirka.TimelineError, match="after its valid_to"):
        esbirka.build_timeline([version("2017-01-01", "a", "2014-01-01")])


def test_mixing_two_provisions_is_refused():
    with pytest.raises(esbirka.TimelineError, match="group_by_provision"):
        esbirka.build_timeline(
            [version("2014-01-01", "a"), version("2017-01-01", "b", section="TEST-2")]
        )


def test_an_empty_timeline_is_empty_not_an_error():
    assert esbirka.build_timeline([]) == []


def test_group_by_provision_buckets_on_the_full_key():
    records = [
        version("2014-01-01", "a"),
        version("2017-01-01", "b"),
        version("2014-01-01", "c", subsec="2"),
        version("2014-01-01", "d", section="TEST-2"),
    ]
    grouped = esbirka.group_by_provision(records)
    assert len(grouped) == 3
    assert len(grouped[(ACT, SECTION, "1")]) == 2


# ==================================================================== the live API
#
# CLAUDE.md rule 1 again, from the other side: the identifiers below — act 89/2012, § 1180,
# § 2000, the effective dates, the amending act 163/2020 Sb. — are *not* invented and are
# not TEST- prefixed, because they were read out of e-Sbírka responses that are on disk in
# `data/raw/ESBIRKA/`. 89/2012 and § 2000 are also the example PLAN.md section 6 prints.

#: The act these tests replay. One real act, cached in full for two of its sections.
ACT_NO = "89/2012"


class _NoNetwork(httpx.Client):
    """An HTTP client that refuses to be one.

    Every API test drives a real :class:`~jg.crawl.base.Fetcher` over this, so "the cache
    replays" is not asserted about the fetcher — it is the only way the test can pass.
    """

    def request(self, *args: object, **kwargs: object) -> httpx.Response:  # type: ignore[override]
        raise AssertionError(f"network request issued in a test: {args[:2]}")


def cached_fetcher() -> Fetcher:
    return Fetcher(esbirka.SOURCE_CODE, client=_NoNetwork())


def require_cached(*urls: str) -> Fetcher:
    """A cache-only fetcher, or a skip naming the first URL that is not on disk."""
    fetcher = cached_fetcher()
    for url in urls:
        if not fetcher.is_cached(url):
            fetcher.close()
            pytest.skip(
                f"no cached {url} under {config.RAW_DIR / esbirka.SOURCE_CODE}; "
                f"run `python -m jg.provisions fetch --act {ACT_NO} --section 1180 "
                "--section 2000 --all-wordings`"
            )
    return fetcher


HISTORY_URL = esbirka.api_url(esbirka.act_eli(ACT_NO), "historie")


# --- URL construction: the one place an invented identifier could enter ------------


def test_act_eli_is_built_from_the_act_number_the_sbirka_prints():
    assert esbirka.act_eli(ACT_NO) == "/eli/cz/sb/2012/89"


@pytest.mark.parametrize(
    "bad", ["89-2012", "2012/89/1", "89/12", "zákon 89/2012", "", "89/2012 Sb."]
)
def test_an_act_number_that_is_not_number_slash_year_is_refused(bad):
    """Coercing it would address a different statute, silently and with full confidence."""
    with pytest.raises(ValueError, match="act number"):
        esbirka.act_eli(bad)


def test_a_date_selects_the_wording_in_force_on_it():
    """The mechanism the whole milestone hangs on: the ELI path takes a date."""
    assert esbirka.wording_stale_url(ACT_NO, dt.date(2014, 1, 1)) == (
        "/eli/cz/sb/2012/89/2014-01-01"
    )
    assert esbirka.wording_stale_url(ACT_NO) == "/eli/cz/sb/2012/89"


def test_the_stale_url_is_encoded_into_one_path_segment():
    """The API answers a raw `/` inside the segment with NEPLATNE_STALE_URL."""
    url = esbirka.api_url("/eli/cz/sb/2012/89", "fragmenty", cisloStranky=0)
    assert url == (
        f"{esbirka.API_BASE_URL}/dokumenty-sbirky/"
        "%2Feli%2Fcz%2Fsb%2F2012%2F89/fragmenty?cisloStranky=0"
    )


def test_a_stale_url_without_a_leading_slash_is_refused_before_it_is_sent():
    with pytest.raises(ValueError, match="must start with"):
        esbirka.api_url("eli/cz/sb/2012/89", "id")


# --- /historie ---------------------------------------------------------------------


def test_history_parses_the_response_actually_on_disk():
    fetcher = require_cached(HISTORY_URL)
    with fetcher:
        wordings = esbirka.fetch_history(fetcher, ACT_NO)
    assert len(wordings) > 1
    assert [w.valid_from for w in wordings] == sorted(w.valid_from for w in wordings)
    assert all(w.act_no == ACT_NO for w in wordings)


def test_the_promulgated_wording_is_kept_off_the_timeline():
    """The trap this filter exists for.

    ``/historie`` returns two entries the timeline must not contain: ``VYHLASENE``, the act
    as published, which starts at the publication date and has **no end date at all**, and
    ``MINULE_NEUCINNE``, the same text dated up to the day before the act took effect.
    ``build_timeline`` would not reject the open one — it looks exactly like a legitimate
    current wording — it would clamp it, and the 2012 text would quietly become the wording
    in force for the next eight years.
    """
    fetcher = require_cached(HISTORY_URL)
    with fetcher:
        wordings = esbirka.fetch_history(fetcher, ACT_NO)
    kinds = {w.kind for w in wordings}
    assert "VYHLASENE" in kinds, "the trap is gone from the response; re-check the filter"
    on_timeline = [w for w in wordings if w.on_timeline]
    assert [w for w in on_timeline if w.valid_to is None] != []
    assert len([w for w in on_timeline if w.valid_to is None]) == 1
    assert all(w.kind not in esbirka.NON_TIMELINE_WORDING_KINDS for w in on_timeline)


def test_the_history_windows_are_contiguous_and_end_inclusive():
    """`datumUcinnostiZneniDo` is the last day, not the first excluded one.

    That is what makes ``valid_to`` storable as-is: a window ends 2020-06-30 and the next
    opens 2020-07-01, which is precisely what ``VERSION_IN_FORCE``'s ``valid_to >= :date``
    expects.
    """
    fetcher = require_cached(HISTORY_URL)
    with fetcher:
        wordings = [w for w in esbirka.fetch_history(fetcher, ACT_NO) if w.on_timeline]
    for earlier, later in zip(wordings, wordings[1:], strict=False):
        assert earlier.valid_to is not None
        assert earlier.valid_to + dt.timedelta(days=1) == later.valid_from


def test_wording_in_force_asks_the_same_question_as_the_java_lateral_join():
    fetcher = require_cached(HISTORY_URL)
    with fetcher:
        wordings = [w for w in esbirka.fetch_history(fetcher, ACT_NO) if w.on_timeline]
    first = wordings[0]
    assert esbirka.wording_in_force(wordings, first.valid_from) == first
    assert esbirka.wording_in_force(wordings, first.valid_to) == first
    assert esbirka.wording_in_force(wordings, first.valid_to + dt.timedelta(days=1)) != first
    assert esbirka.wording_in_force(wordings, first.valid_from - dt.timedelta(days=1)) is None


def test_parse_history_refuses_a_wording_with_no_start_date():
    with pytest.raises(esbirka.EsbirkaApiError, match="datumUcinnostiZneniOd"):
        esbirka.parse_history({"historie": [{"typZneni": "MINULE"}]}, ACT_NO)


def test_parse_history_refuses_a_response_it_does_not_recognise():
    with pytest.raises(esbirka.EsbirkaApiError, match="historie"):
        esbirka.parse_history({"vysledky": []}, ACT_NO)


# --- the act number is read back off the source, not echoed ------------------------


def test_the_act_number_stored_is_the_one_e_sbirka_prints():
    assert esbirka.act_no_from_detail({"citace": "89/2012 Sb."}) == ACT_NO


@pytest.mark.parametrize("detail", [{}, {"citace": ""}, {"citace": "občanský zákoník"}])
def test_an_unconfirmable_act_number_raises_rather_than_falling_back(detail):
    """Falling back on the requested number would make the confirmation a no-op."""
    with pytest.raises(esbirka.EsbirkaApiError):
        esbirka.act_no_from_detail(detail)


# --- fragments: ELI -> (section, subsec) -------------------------------------------
#
# Every ELI below was copied out of a cached /fragmenty response for act 89/2012.


@pytest.mark.parametrize(
    ("eli", "expected"),
    [
        ("/eli/cz/sb/2012/89/2014-01-01/dokument/norma/cast_1/hlava_2/dil_3/oddil_3/"
         "pododdil_2/par_309", ("309", None)),
        ("/eli/cz/sb/2012/89/2014-01-01/dokument/norma/cast_1/hlava_2/dil_3/oddil_3/"
         "pododdil_2/par_309/odst_1", ("309", "1")),
        ("/eli/cz/sb/2012/89/2014-01-01/dokument/norma/cast_3/hlava_2/dil_4/oddil_5/"
         "pododdil_4/par_1170/odst_2/pism_b", ("1170", "2")),
        ("/eli/cz/sb/2012/89/2014-01-01/dokument/norma/cast_3/hlava_2/dil_4/oddil_5/"
         "pododdil_4/par_1176/frag_7984379", ("1176", None)),
        ("/eli/cz/sb/2012/89/2014-01-01/dokument/norma/cast_3", (None, None)),
    ],
)
def test_the_provision_key_is_read_out_of_the_eli_path(eli, expected):
    """The structural path, not the printed citation: a path cannot disagree with itself.

    A písmeno folds into the odstavec it sits under, because ``provision`` has no column
    below ``subsec`` — and *§ 1170 odst. 2* is how such a rule is cited anyway.
    """
    assert esbirka.fragment_keys(eli) == expected


def test_fragment_text_keeps_the_odstavec_marker_and_drops_the_markup():
    """`<var>(1)</var>` is part of how the provision reads; `<czechvoc-termin>` is not."""
    xhtml = (
        '<var>(1)</var> Vlastník <czechvoc-termin koncept-id="260231">jednotky</'
        "czechvoc-termin> přispívá na správu domu."
    )
    assert esbirka.fragment_text(xhtml) == "(1) Vlastník jednotky přispívá na správu domu."
    assert esbirka.fragment_text("<var>§ 1184</var>") == "§ 1184"
    assert esbirka.fragment_text("") == ""


def test_a_fragment_page_response_without_a_page_count_is_refused():
    """`pocetStranek` is the loop bound; defaulting it would read exactly one page."""
    with pytest.raises(esbirka.EsbirkaApiError, match="pocetStranek"):
        esbirka.parse_fragment_page({"seznam": []})


def test_the_first_fragment_page_is_page_zero():
    """`cisloStranky` is 0-based. A 1-based loop drops §§ 1-308 of the civil code without
    any error at all — page 1 starts at § 309 — and page `pocetStranek` is an HTTP 400."""
    url = esbirka.api_url(
        esbirka.wording_stale_url(ACT_NO, dt.date(2014, 1, 1)), "fragmenty", cisloStranky=0
    )
    fetcher = require_cached(url)
    with fetcher:
        fragments, pages = esbirka.parse_fragment_page(esbirka.fetch_api(fetcher, url))
    assert pages > 1
    assert fragments
    sections = [f.section for f in fragments if f.section is not None]
    assert sections and sections[0] == "1"


# --- assembly ----------------------------------------------------------------------


def fragment(eli: str, text: str) -> esbirka.Fragment:
    section, subsec = esbirka.fragment_keys(eli)
    return esbirka.Fragment(
        id=0, eli=eli, kind="Odstavec_Dc", citation="", text=text,
        section=section, subsec=subsec,
    )


def test_a_section_yields_both_the_whole_paragraph_and_each_subsection():
    """A citation may name either, and `provision` keys them as different rows."""
    base = "/eli/cz/sb/2012/89/2014-01-01/dokument/norma/cast_4/par_2000"
    wording = esbirka.Wording(
        act_no=ACT_NO, valid_from=dt.date(2014, 1, 1), valid_to=None, kind="AKTUALNI"
    )
    records = esbirka.versions_from_fragments(
        wording,
        [
            fragment(base, "§ 2000"),
            fragment(f"{base}/odst_1", "(1) první"),
            fragment(f"{base}/odst_2", "(2) druhý"),
        ],
    )
    by_key = {r.provision.key(): r.body for r in records}
    assert by_key[(ACT_NO, "2000", None)] == "§ 2000\n(1) první\n(2) druhý"
    assert by_key[(ACT_NO, "2000", "1")] == "(1) první"
    assert by_key[(ACT_NO, "2000", "2")] == "(2) druhý"
    assert all(r.valid_from == dt.date(2014, 1, 1) and r.valid_to is None for r in records)


def test_fragments_of_other_sections_are_left_out():
    wording = esbirka.Wording(
        act_no=ACT_NO, valid_from=dt.date(2014, 1, 1), kind="AKTUALNI"
    )
    stem = "/eli/cz/sb/2012/89/2014-01-01/dokument/norma/cast_4"
    records = esbirka.versions_from_fragments(
        wording,
        [fragment(f"{stem}/par_2000", "§ 2000"), fragment(f"{stem}/par_2001", "§ 2001")],
        sections=["2000"],
    )
    assert {r.provision.section for r in records} == {"2000"}


# --- the whole fetch, replayed from disk -------------------------------------------


def test_the_section_10_fetch_returns_two_different_wordings_from_the_cache():
    """M6, end to end and offline: one § read at two dates comes back as two wordings.

    § 1180 of the civil code — a unit owner's contribution to the management of the
    building — was reworded with effect from 2020-07-01 by 163/2020 Sb. A decision from
    2016 that relies on it has a spotless citation history and is reasoning about a rule
    that no longer reads that way, which is exactly the case PLAN.md section 10 exists to
    catch and which no keyword system finds.
    """
    fetcher = require_cached(HISTORY_URL)
    with fetcher:
        records = esbirka.fetch_provision_versions(
            fetcher,
            ACT_NO,
            ["1180"],
            dates=[dt.date(2016, 3, 15), dt.date(2026, 9, 3)],
        )
    bodies = {
        r.valid_from: r.body for r in records if r.provision.key() == (ACT_NO, "1180", "1")
    }
    assert len(bodies) == 2, f"expected two wordings, got {sorted(bodies)}"
    earlier, later = (bodies[key] for key in sorted(bodies))
    assert earlier != later
    assert "Nebylo-li jinak určeno" in earlier
    assert "v poměru odpovídajícím" in later


def test_the_fetched_wordings_close_into_a_timeline_with_one_open_window():
    fetcher = require_cached(HISTORY_URL)
    with fetcher:
        records = esbirka.fetch_provision_versions(
            fetcher, ACT_NO, ["1180"], all_wordings=True
        )
    group = [r for r in records if r.provision.key() == (ACT_NO, "1180", "1")]
    timeline = esbirka.build_timeline(group)
    assert len(timeline) == 2, "163/2020 Sb. is the only amendment to § 1180 odst. 1"
    assert [v.valid_to is None for v in timeline] == [False, True]
    assert timeline[0].valid_to + dt.timedelta(days=1) == timeline[1].valid_from
    assert timeline[0].valid_from == dt.date(2014, 1, 1), "the act took effect 2014-01-01"


def test_a_fetch_over_dates_inside_one_wording_costs_one_wording():
    """Two questions about the same wording are one fetch, not two rows that compare equal."""
    fetcher = require_cached(HISTORY_URL)
    with fetcher:
        records = esbirka.fetch_provision_versions(
            fetcher, ACT_NO, ["1180"], dates=[dt.date(2016, 3, 15), dt.date(2016, 6, 1)]
        )
    assert {r.valid_from for r in records} == {dt.date(2014, 1, 1)}


def test_a_section_that_never_changed_yields_one_version():
    """§ 2000 — the section PLAN.md section 6 names — is untouched across the wordings,
    so the coalescing in `build_timeline` has to collapse several records into one.

    This is the case that keeps a republication from reading as a rewording, and the whole
    amber hangs on ``v0.body <> v1.body``. Four sampled dates rather than the full history:
    the point is that identical bodies coalesce, and four wordings prove it as well as
    nineteen at a fifth of the parse cost.
    """
    fetcher = require_cached(HISTORY_URL)
    with fetcher:
        records = esbirka.fetch_provision_versions(
            fetcher,
            ACT_NO,
            ["2000"],
            dates=[
                dt.date(2014, 1, 1),
                dt.date(2018, 1, 1),
                dt.date(2021, 1, 1),
                dt.date(2026, 9, 3),
            ],
        )
    group = [r for r in records if r.provision.key() == (ACT_NO, "2000", "1")]
    assert len(group) > 1, "several wordings were read"
    timeline = esbirka.build_timeline(group)
    assert len(timeline) == 1
    assert timeline[0].valid_to is None

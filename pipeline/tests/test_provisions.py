"""Provision ingest, timeline queries and the materiality gate. PLAN.md section 10, M6.

Two halves.

**Pure** — prompt rendering, the evidence-span gate, the cache key. No database, no key.

**Against Postgres** — the ingest and the timeline queries, because the thing being proved
is that ``ProvisionRepository.VERSION_IN_FORCE`` returns exactly one row per date against
rows this pipeline wrote, and that cannot be proved against a fake. Every database test
runs inside a transaction that is rolled back, so the suite leaves no rows behind, and the
whole group skips cleanly where no Postgres is listening.

CLAUDE.md rule 1: every identifier here carries a ``TEST-`` prefix and cannot resolve. The
wordings are synthetic Czech, not quoted statute.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from jg import provisions
from jg.classify.reasoning import ResponseRejected
from jg.crawl.esbirka import ProvisionRecord, ProvisionVersionRecord

ACT = "TEST-000/0000"

#: Unique per run, so these tests assert nothing about what is already in the database.
#: The demo rows an operator ingests, and any other agent's leftovers, share the schema;
#: a test that only passes against an empty table is a test that will fail on a Tuesday.
#: Still ``TEST-`` prefixed, so it cannot resolve against the Sbírka (CLAUDE.md rule 1).
SECTION = f"TEST-{uuid.uuid4().hex[:12]}"

#: Two synthetic wordings differing in one condition, so a materiality prompt built from
#: them is a real prompt rather than a placeholder.
BODY_2014 = "Nájemce je povinen oznámit pronajímateli vadu bez zbytečného odkladu."
BODY_2017 = "Nájemce je povinen oznámit pronajímateli vadu do třiceti dnů."


def record(
    valid_from: str,
    body: str,
    valid_to: str | None = None,
    *,
    subsec: str | None = "1",
    derogated_by: str | None = None,
) -> ProvisionVersionRecord:
    return ProvisionVersionRecord(
        provision=ProvisionRecord(act_no=ACT, section=SECTION, subsec=subsec),
        body=body,
        valid_from=dt.date.fromisoformat(valid_from),
        valid_to=dt.date.fromisoformat(valid_to) if valid_to else None,
        derogated_by=derogated_by,
    )


TIMELINE = [record("2014-01-01", BODY_2014), record("2017-07-01", BODY_2017)]


# ============================================================== pure: the span gate


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "material": True,
        "confidence": 0.9,
        "evidence_span": "do třiceti dnů",
        "reasoning": "Lhůta byla nahrazena pevným počtem dnů.",
    }
    payload.update(overrides)
    return payload


def test_a_span_quoted_from_the_later_wording_is_accepted():
    material, confidence, span = provisions.parse_materiality(
        _payload(), BODY_2014, BODY_2017
    )
    assert (material, confidence, span) == (True, 0.9, "do třiceti dnů")


def test_a_span_quoted_from_the_earlier_wording_is_accepted():
    """The frozen prompt says "either wording", so both are checked."""
    _, _, span = provisions.parse_materiality(
        _payload(evidence_span="bez zbytečného odkladu"), BODY_2014, BODY_2017
    )
    assert span == "bez zbytečného odkladu"


def test_a_span_present_in_neither_wording_is_rejected():
    with pytest.raises(ResponseRejected, match="occurs in neither wording"):
        provisions.parse_materiality(
            _payload(evidence_span="do šedesáti dnů"), BODY_2014, BODY_2017
        )


def test_whitespace_is_normalised_on_both_sides_but_nothing_else_is():
    """A model that re-types a line break is quoting correctly; one that drops a diacritic
    is not, because in Czech legal prose the diacritic carries the meaning."""
    _, _, span = provisions.parse_materiality(
        _payload(evidence_span="do  třiceti\n dnů"), BODY_2014, BODY_2017
    )
    assert span == "do třiceti dnů"
    with pytest.raises(ResponseRejected):
        provisions.parse_materiality(
            _payload(evidence_span="do triceti dnu"), BODY_2014, BODY_2017
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"material": "ano"},
        {"material": None},
        {"confidence": "vysoká"},
        {"confidence": True},
        {"confidence": 1.4},
        {"confidence": -0.1},
        {"evidence_span": ""},
        {"evidence_span": "   "},
        {"evidence_span": None},
    ],
)
def test_a_malformed_response_is_rejected(bad):
    with pytest.raises(ResponseRejected):
        provisions.parse_materiality(_payload(**bad), BODY_2014, BODY_2017)


def test_the_rejection_message_is_usable_as_a_retry_instruction():
    with pytest.raises(ResponseRejected) as excinfo:
        provisions.parse_materiality(
            _payload(evidence_span="vymyšlený text"), BODY_2014, BODY_2017
        )
    retry = provisions.retry_prompt("PROMPT", excinfo.value.violation)
    assert "character for character" in retry
    assert "vymyšlený text" in retry


# ============================================================ pure: prompt and cache


def test_the_prompt_renders_the_frozen_template_with_both_wordings():
    prompt = provisions.build_materiality_prompt(
        ProvisionRecord(act_no=ACT, section=SECTION, subsec="1"),
        BODY_2014,
        BODY_2017,
        dt.date(2014, 1, 1),
        dt.date(2017, 7, 1),
    )
    assert "{{" not in prompt
    assert BODY_2014 in prompt and BODY_2017 in prompt
    assert f"§ {SECTION} odst. 1 zákona č. {ACT} Sb." in prompt
    assert "2014-01-01" in prompt and "2017-07-01" in prompt


def test_a_provision_with_no_subsection_still_renders():
    """`PromptTemplate.render` rejects None, so the caller has to say what None looks like."""
    prompt = provisions.build_materiality_prompt(
        ProvisionRecord(act_no=ACT, section=SECTION, subsec=None),
        BODY_2014,
        BODY_2017,
        dt.date(2014, 1, 1),
        dt.date(2017, 7, 1),
    )
    assert "{{" not in prompt
    assert "odst." not in prompt.split("## Wording")[0]


def test_the_cache_key_is_the_version_pair_not_the_decision():
    """PLAN.md section 10: many decisions share a provision, so the pair is the key."""
    assert provisions.cache_key(7, 9, "provision-materiality.v1") == (
        "materiality|7|9|provision-materiality.v1"
    )


def test_the_prompt_version_written_to_rows_is_the_frozen_one():
    from jg.classify.prompts import prompt_template

    assert prompt_template(provisions.PROMPT_NAME).version == "provision-materiality.v1"


# ================================================================= against Postgres


@pytest.fixture
def conn():
    """A rolled-back psycopg connection, or a skip."""
    import psycopg
    from psycopg.rows import dict_row

    from jg.db import dsn

    try:
        connection = psycopg.connect(dsn(), row_factory=dict_row, connect_timeout=3)
    except psycopg.OperationalError as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"no Postgres at {dsn()}: {exc}")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def provision_id(conn):
    stats = provisions.ingest_records(conn, TIMELINE)
    assert stats.provisions_created == 1
    row = conn.execute(
        "select id from provision where act_no = %s and section = %s "
        "and subsec is not distinct from %s",
        (ACT, SECTION, "1"),
    ).fetchone()
    return int(row["id"])


def _stored(conn, provision_id):
    return conn.execute(
        "select id, body, valid_from, valid_to, derogated_by from provision_version "
        "where provision_id = %s order by valid_from",
        (provision_id,),
    ).fetchall()


def test_ingest_writes_a_closed_ordered_timeline(conn, provision_id):
    rows = _stored(conn, provision_id)
    assert [(r["valid_from"], r["valid_to"]) for r in rows] == [
        (dt.date(2014, 1, 1), dt.date(2017, 6, 30)),
        (dt.date(2017, 7, 1), None),
    ]
    assert provisions.timeline_problems(conn, provision_id) == []


def test_version_in_force_answers_the_java_question_at_two_dates(conn, provision_id):
    """The whole of PLAN.md section 10 in one assertion: the wording moved underneath."""
    at_decision = provisions.version_in_force(conn, provision_id, dt.date(2015, 3, 12))
    at_query = provisions.version_in_force(conn, provision_id, dt.date(2026, 9, 2))
    assert at_decision["body"] == BODY_2014
    assert at_query["body"] == BODY_2017
    assert at_decision["id"] != at_query["id"]


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ("2013-12-31", None),
        ("2014-01-01", BODY_2014),
        ("2017-06-30", BODY_2014),
        ("2017-07-01", BODY_2017),
        ("2030-01-01", BODY_2017),
    ],
)
def test_every_date_resolves_to_at_most_one_version(conn, provision_id, probe, expected):
    """Boundary days included: `valid_to` is inclusive, so 2017-06-30 is still the old text."""
    row = provisions.version_in_force(conn, provision_id, dt.date.fromisoformat(probe))
    assert (row["body"] if row else None) == expected


def test_ingesting_twice_changes_nothing_and_keeps_the_row_ids(conn, provision_id):
    """Id stability is the point: provision_materiality is keyed on provision_version.id."""
    before = [(r["id"], r["valid_from"], r["valid_to"]) for r in _stored(conn, provision_id)]
    stats = provisions.ingest_records(conn, TIMELINE)
    after = [(r["id"], r["valid_from"], r["valid_to"]) for r in _stored(conn, provision_id)]
    assert before == after
    assert stats.provisions_created == 0
    assert (stats.versions_inserted, stats.versions_updated, stats.versions_deleted) == (0, 0, 0)
    assert stats.versions_unchanged == 2


def test_a_later_amendment_extends_the_timeline_without_disturbing_the_earlier_rows(
    conn, provision_id
):
    before = {r["valid_from"]: r["id"] for r in _stored(conn, provision_id)}
    third = "Nájemce je povinen oznámit pronajímateli vadu do patnácti dnů."
    provisions.ingest_records(conn, [*TIMELINE, record("2021-01-01", third)])
    rows = _stored(conn, provision_id)
    assert [(r["valid_from"], r["valid_to"]) for r in rows] == [
        (dt.date(2014, 1, 1), dt.date(2017, 6, 30)),
        (dt.date(2017, 7, 1), dt.date(2020, 12, 31)),
        (dt.date(2021, 1, 1), None),
    ]
    assert rows[0]["id"] == before[dt.date(2014, 1, 1)]
    assert rows[1]["id"] == before[dt.date(2017, 7, 1)]
    assert provisions.timeline_problems(conn, provision_id) == []


def test_a_corrected_wording_updates_in_place(conn, provision_id):
    corrected = BODY_2017 + " Oznámení vyžaduje písemnou formu."
    before = {r["valid_from"]: r["id"] for r in _stored(conn, provision_id)}
    stats = provisions.ingest_records(
        conn, [TIMELINE[0], record("2017-07-01", corrected)]
    )
    rows = _stored(conn, provision_id)
    assert stats.versions_updated == 1
    assert rows[1]["id"] == before[dt.date(2017, 7, 1)]
    assert rows[1]["body"] == corrected


def test_timeline_problems_sees_an_overlap_the_query_would_silently_hide(conn, provision_id):
    """Written directly, bypassing the ingest, because the ingest cannot produce it."""
    conn.execute(
        "update provision_version set valid_to = null where provision_id = %s "
        "and valid_from = %s",
        (provision_id, dt.date(2014, 1, 1)),
    )
    problems = provisions.timeline_problems(conn, provision_id)
    assert any("overlaps" in p for p in problems)
    assert any("valid_to null" in p for p in problems)


def test_a_derogation_naming_an_uncrawled_decision_is_refused_not_dropped(conn):
    with pytest.raises(provisions.IngestError, match="not in the corpus"):
        provisions.ingest_records(
            conn,
            [record("2014-01-01", BODY_2014),
             record("2017-07-01", BODY_2017, derogated_by="TEST-ECLI:NEEXISTUJE")],
        )


def test_a_version_a_materiality_verdict_points_at_is_not_silently_deleted(conn, provision_id):
    rows = _stored(conn, provision_id)
    conn.execute(
        "insert into provision_materiality (from_version_id, to_version_id, prompt_version, "
        "material, confidence, evidence_span) values (%s, %s, %s, %s, %s, %s)",
        (rows[0]["id"], rows[1]["id"], "provision-materiality.v1", True, 0.9, "do třiceti dnů"),
    )
    with pytest.raises(provisions.IngestError, match="provision_materiality verdict"):
        provisions.ingest_records(conn, [record("2019-01-01", BODY_2017)])


# ------------------------------------------------- the amber, end to end, no API key


@pytest.fixture
def relying_decision(conn, provision_id):
    """A TEST- decision from 2015 that cites the provision. Rolled back with the fixture."""
    ecli = f"TEST-ECLI:M6:{uuid.uuid4().hex[:12]}"
    conn.execute(
        "insert into decision (ecli, court_code, panel_type, decided_on, source_url, "
        "fetched_at) values (%s, %s, %s, %s, %s, now())",
        (ecli, "NSS", "panel", dt.date(2015, 3, 12), "https://vyhledavac.nssoud.cz/TEST"),
    )
    conn.execute(
        "insert into citation (citing_ecli, cited_provision, paragraph_idx, raw_text, "
        "extractor) values (%s, %s, %s, %s, %s)",
        (ecli, provision_id, 12, f"§ {SECTION} odst. 1 zákona č. {ACT} Sb.", "rules"),
    )
    return ecli


def _ours(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only this test's own pending pairs. Other rows in the database are not our business."""
    return [pair for pair in pairs if pair["section"] == SECTION]


def _verdicts(conn, provision_id) -> list[dict[str, Any]]:
    return conn.execute(
        "select m.material, m.confidence, m.evidence_span, m.prompt_version "
        "  from provision_materiality m "
        "  join provision_version v on v.id = m.from_version_id "
        " where v.provision_id = %s",
        (provision_id,),
    ).fetchall()


def test_a_rewording_under_a_decision_shows_up_as_pending(conn, relying_decision):
    [pair] = _ours(provisions.pending_rewordings(conn, dt.date(2026, 9, 2)))
    assert pair["from_body"] == BODY_2014
    assert pair["to_body"] == BODY_2017
    assert pair["act_no"] == ACT


def test_nothing_is_pending_when_the_question_predates_the_amendment(conn, relying_decision):
    assert _ours(provisions.pending_rewordings(conn, dt.date(2016, 1, 1))) == []


def test_a_validated_verdict_is_stored_with_its_span(conn, relying_decision, provision_id):
    calls: list[str] = []

    def model(prompt: str) -> Mapping[str, Any]:
        calls.append(prompt)
        return _payload()

    for pair in _ours(provisions.pending_rewordings(conn, dt.date(2026, 9, 2))):
        assert provisions.judge_materiality(conn, pair, model=model) is not None
    assert len(calls) == 1
    [row] = _verdicts(conn, provision_id)
    assert row["material"] is True
    assert row["evidence_span"] == "do třiceti dnů"
    assert row["prompt_version"] == "provision-materiality.v1"


def test_a_bad_span_is_retried_once_and_then_not_stored(conn, relying_decision, provision_id):
    """CLAUDE.md rule 3. There is no UNCLASSIFIED column here, so the row is simply absent,
    and ProvisionRepository reads an absent row as `material = false`."""
    attempts: list[str] = []

    def model(prompt: str) -> Mapping[str, Any]:
        attempts.append(prompt)
        return _payload(evidence_span="tato věta v žádném znění není")

    [pair] = _ours(provisions.pending_rewordings(conn, dt.date(2026, 9, 2)))
    assert provisions.judge_materiality(conn, pair, model=model) is None
    assert len(attempts) == 2
    assert "was rejected" in attempts[1]
    assert _verdicts(conn, provision_id) == []


def test_a_second_attempt_that_validates_is_stored(conn, relying_decision, provision_id):
    responses = [_payload(evidence_span="nesmysl"), _payload()]

    def model(prompt: str) -> Mapping[str, Any]:
        return responses.pop(0)

    [pair] = _ours(provisions.pending_rewordings(conn, dt.date(2026, 9, 2)))
    assert provisions.judge_materiality(conn, pair, model=model) is not None
    assert len(_verdicts(conn, provision_id)) == 1


def test_a_judged_rewording_stops_being_pending(conn, relying_decision):
    for pair in _ours(provisions.pending_rewordings(conn, dt.date(2026, 9, 2))):
        provisions.judge_materiality(conn, pair, model=lambda _prompt: _payload())
    assert _ours(provisions.pending_rewordings(conn, dt.date(2026, 9, 2))) == []


def test_the_second_run_is_served_from_the_cache(conn, relying_decision):
    """Keyed on the version pair, so the many decisions sharing a provision pay once."""
    calls: list[str] = []

    def model(prompt: str) -> Mapping[str, Any]:
        calls.append(prompt)
        return _payload()

    [pair] = _ours(provisions.pending_rewordings(conn, dt.date(2026, 9, 2)))
    provisions.judge_materiality(conn, pair, model=model)
    provisions.judge_materiality(conn, pair, model=model)
    assert len(calls) == 1


def test_a_poisoned_cache_row_cannot_smuggle_a_verdict_past_the_gate(conn, relying_decision):
    from psycopg.types.json import Json

    [pair] = _ours(provisions.pending_rewordings(conn, dt.date(2026, 9, 2)))
    key = provisions.cache_key(
        pair["from_version_id"], pair["to_version_id"], "provision-materiality.v1"
    )
    conn.execute(
        "insert into llm_cache (cache_key, prompt_version, model, request, response) "
        "values (%s, %s, %s, %s, %s)",
        (key, "provision-materiality.v1", "TEST-model", Json({}),
         Json(_payload(evidence_span="tato věta v žádném znění není"))),
    )
    calls: list[str] = []

    def model(prompt: str) -> Mapping[str, Any]:
        calls.append(prompt)
        return _payload()

    verdict = provisions.judge_materiality(conn, pair, model=model)
    assert verdict is not None
    assert verdict.evidence_span == "do třiceti dnů"
    assert len(calls) == 1, "the rejected cache row must have been re-asked, not trusted"


# ============================ real statute, real Postgres, no network, rolled back
#
# Everything above this line is synthetic and TEST- prefixed. These are not: they ingest
# actual e-Sbírka wordings of act 89/2012, replayed from `data/raw/ESBIRKA/`, because the
# claim milestone M6 has to support is about statute that really was reworded and a
# synthetic fixture cannot support it. The identifiers are real *because they came out of
# the source* (CLAUDE.md rule 1), and every row is written inside the rolled-back `conn`
# fixture, so these tests leave nothing behind either.

ACT_89_2012 = "89/2012"

#: § 1180 of the civil code: a unit owner's contribution to managing the building and the
#: land. Reworded with effect from 2020-07-01. A decision from 2016 relying on it has a
#: spotless citation history and is reasoning about a rule that no longer reads that way,
#: which is the case PLAN.md section 10 exists to catch.
REWORDED_SECTION = "1180"

#: The section PLAN.md section 6 names, untouched across every wording of the act. Present
#: so that "one § moved and the other did not" is asserted from the same fetch.
STABLE_SECTION = "2000"


@pytest.fixture(scope="module")
def real_versions():
    """Wordings of §§ 1180 and 2000 fetched once, from the cache, for the whole module.

    The HTTP client raises on any call, so this fixture is also the proof that a re-run
    issues zero network requests (CLAUDE.md rule 5). Module-scoped because the parse is the
    expensive part and the records are immutable; the *writes* still happen per test,
    inside that test's own rolled-back transaction.
    """
    import httpx

    from jg.crawl import esbirka
    from jg.crawl.base import Fetcher

    class NoNetwork(httpx.Client):
        def request(self, *args: object, **kwargs: object):  # type: ignore[override]
            raise AssertionError(f"network request issued in a test: {args[:2]}")

    fetcher = Fetcher(esbirka.SOURCE_CODE, client=NoNetwork())
    url = esbirka.api_url(esbirka.act_eli(ACT_89_2012), "historie")
    if not fetcher.is_cached(url):
        fetcher.close()
        pytest.skip(
            f"no cached e-Sbírka responses for act {ACT_89_2012}; run "
            f"`python -m jg.provisions fetch --act {ACT_89_2012} "
            f"--section {REWORDED_SECTION} --section {STABLE_SECTION} --all-wordings`"
        )
    with fetcher:
        return esbirka.fetch_provision_versions(
            fetcher,
            ACT_89_2012,
            [REWORDED_SECTION, STABLE_SECTION],
            all_wordings=True,
        )


@pytest.fixture
def real_provisions(conn, real_versions):
    """The fetched wordings written to Postgres. Returns ``(act, section, subsec) -> id``."""
    provisions.ingest_records(conn, real_versions)
    rows = conn.execute(
        "select id, act_no, section, subsec from provision where act_no = %s "
        "and section = any(%s)",
        (ACT_89_2012, [REWORDED_SECTION, STABLE_SECTION]),
    ).fetchall()
    return {(r["act_no"], r["section"], r["subsec"]): int(r["id"]) for r in rows}


def test_a_real_statutory_timeline_is_written_closed_and_ordered(conn, real_provisions):
    """The milestone, end to end: fetch -> close the windows -> rows Postgres can answer."""
    provision_id = real_provisions[(ACT_89_2012, REWORDED_SECTION, "1")]
    rows = _stored(conn, provision_id)
    assert [(r["valid_from"], r["valid_to"]) for r in rows] == [
        (dt.date(2014, 1, 1), dt.date(2020, 6, 30)),
        (dt.date(2020, 7, 1), None),
    ], "the act took effect 2014-01-01 and § 1180 was reworded from 2020-07-01"
    assert provisions.timeline_problems(conn, provision_id) == []


def test_a_section_nothing_amended_keeps_one_open_version(conn, real_provisions):
    """Nineteen wordings, one row: a republication must not read as a rewording, because
    the whole amber hangs on `v0.body <> v1.body`."""
    provision_id = real_provisions[(ACT_89_2012, STABLE_SECTION, "1")]
    rows = _stored(conn, provision_id)
    assert len(rows) == 1
    assert rows[0]["valid_to"] is None
    assert provisions.timeline_problems(conn, provision_id) == []


def test_the_wording_moved_underneath_a_2016_decision(conn, real_provisions):
    """PLAN.md section 10 as one assertion, against statute rather than a fixture."""
    provision_id = real_provisions[(ACT_89_2012, REWORDED_SECTION, "1")]
    before, after = provisions.wording_change(
        conn, provision_id, dt.date(2016, 3, 15), dt.date(2026, 9, 3)
    )
    assert before is not None and after is not None
    assert before["id"] != after["id"]
    assert before["body"] != after["body"]
    assert "Nebylo-li jinak určeno" in before["body"]
    assert "v poměru odpovídajícím" in after["body"]


def test_a_decision_relying_on_the_stable_section_sees_no_change(conn, real_provisions):
    """The other half of D8: the same query must not manufacture an amber where none is."""
    provision_id = real_provisions[(ACT_89_2012, STABLE_SECTION, "1")]
    before, after = provisions.wording_change(
        conn, provision_id, dt.date(2016, 3, 15), dt.date(2026, 9, 3)
    )
    assert before is not None and after is not None
    assert before["id"] == after["id"]
    assert before["body"] == after["body"]


@pytest.mark.parametrize(
    ("probe", "expected_index"),
    [("2013-12-31", None), ("2014-01-01", 0), ("2020-06-30", 0), ("2020-07-01", 1),
     ("2026-09-03", 1)],
)
def test_every_date_resolves_to_one_real_wording(conn, real_provisions, probe, expected_index):
    """`valid_to` is inclusive, so 2020-06-30 is still the old text and 2013-12-31 is none:
    the act had been published but had not taken effect."""
    provision_id = real_provisions[(ACT_89_2012, REWORDED_SECTION, "1")]
    rows = _stored(conn, provision_id)
    row = provisions.version_in_force(conn, provision_id, dt.date.fromisoformat(probe))
    expected = None if expected_index is None else rows[expected_index]["id"]
    assert (row["id"] if row else None) == expected


def test_re_ingesting_real_statute_changes_nothing(conn, real_provisions, real_versions):
    provision_id = real_provisions[(ACT_89_2012, REWORDED_SECTION, "1")]
    before = [(r["id"], r["valid_from"], r["valid_to"]) for r in _stored(conn, provision_id)]
    stats = provisions.ingest_records(conn, real_versions)
    assert (stats.versions_inserted, stats.versions_updated, stats.versions_deleted) == (0, 0, 0)
    assert stats.provisions_created == 0
    stored = sum(len(_stored(conn, pid)) for pid in set(real_provisions.values()))
    assert stats.versions_unchanged == stored, "every stored version was matched, none rewritten"
    assert [(r["id"], r["valid_from"], r["valid_to"]) for r in _stored(conn, provision_id)] == (
        before
    ), "id stability: provision_materiality is keyed on provision_version.id"


def test_both_a_section_and_its_subsections_are_addressable(conn, real_provisions):
    """A citation may name § 1180 or § 1180 odst. 1, and `provision` keys them separately."""
    assert (ACT_89_2012, REWORDED_SECTION, None) in real_provisions
    assert (ACT_89_2012, REWORDED_SECTION, "1") in real_provisions
    assert (ACT_89_2012, REWORDED_SECTION, "2") in real_provisions
    whole = provisions.version_in_force(
        conn, real_provisions[(ACT_89_2012, REWORDED_SECTION, None)], dt.date(2016, 3, 15)
    )
    subsection = provisions.version_in_force(
        conn, real_provisions[(ACT_89_2012, REWORDED_SECTION, "1")], dt.date(2016, 3, 15)
    )
    assert subsection["body"] in whole["body"]
    assert whole["body"].startswith(f"§ {REWORDED_SECTION}")


def test_ingest_act_refuses_to_pull_a_whole_act(conn):
    """Every row would need a provision identity nothing cites, at eleven pages a wording."""
    with pytest.raises(provisions.IngestError, match="no sections given"):
        provisions.ingest_act(conn, ACT_89_2012, [], all_wordings=True)

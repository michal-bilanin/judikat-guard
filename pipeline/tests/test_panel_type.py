"""Panel-type detection, one pure function per court.

Panel type is the free structural signal M3 runs on: a *rozšířený senát* / *velký senát* /
*plénum* decision is the one body allowed to depart from its own court's case law
(``departure_authority``, PLAN.md section 6), so ``extended``/``grand``/``plenary`` is
what turns a ``DEPARTED`` label into a RED ``Superseded`` rather than an AMBER
``Conflict``. Getting it wrong in the false-positive direction manufactures red lights,
which PLAN.md D8 says is the one thing never to do.

The referral cases matter as much as the positive ones. A three-judge panel that *refers*
a question to the extended panel is still a three-judge panel; the phrase
"postoupil rozšířenému senátu" appears in ``extract/patterns.toml`` as a departure marker
precisely because it shows up in ordinary panel decisions.

No fixtures, no network: the functions take short strings by design.
"""

from __future__ import annotations

import pytest

from jg.crawl import nalus, nsoud, nssoud
from jg.models import PanelType

# ------------------------------------------------------------------------------ NSS


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        (
            "Nejvyšší správní soud rozhodl v rozšířeném senátu složeném z předsedy",
            PanelType.EXTENDED,
        ),
        ("Rozšířený senát Nejvyššího správního soudu rozhodl takto:", PanelType.EXTENDED),
        ("usnesení rozšířeného senátu Nejvyššího správního soudu", PanelType.EXTENDED),
        # Diacritics stripped by the source system must classify identically.
        ("rozsireny senat Nejvyssiho spravniho soudu rozhodl", PanelType.EXTENDED),
        ("Nejvyšší správní soud rozhodl v senátě složeném z předsedy", PanelType.PANEL),
        ("Nejvyšší správní soud rozhodl takto:", PanelType.PANEL),
        # Referrals: the deciding body is the ordinary panel handing the question over.
        ("Nejvyšší správní soud postoupil věc rozšířenému senátu", PanelType.PANEL),
        ("První senát postupuje rozšířenému senátu tuto otázku", PanelType.PANEL),
        ("Věc byla předložena rozšířenému senátu podle § 17 s. ř. s.", PanelType.PANEL),
    ],
)
def test_nss_panel_type_from_heading(heading, expected):
    assert nssoud.panel_type(heading=heading) is expected


def test_nss_panel_type_reads_a_metadata_field():
    assert nssoud.panel_type(decision_kind="rozšířený senát") is PanelType.EXTENDED


def test_nss_panel_type_is_unknown_only_without_input():
    assert nssoud.panel_type() is PanelType.UNKNOWN
    assert nssoud.panel_type(heading=None, case_no="", decision_kind="   ") is PanelType.UNKNOWN


# ------------------------------------------------------------------------------- NS


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        (
            "Nejvyšší soud rozhodl ve velkém senátě občanskoprávního a obchodního kolegia",
            PanelType.GRAND,
        ),
        ("Velký senát trestního kolegia Nejvyššího soudu rozhodl", PanelType.GRAND),
        ("rozsudek velkého senátu Nejvyššího soudu", PanelType.GRAND),
        ("velky senat trestniho kolegia rozhodl", PanelType.GRAND),
        ("Nejvyšší soud rozhodl v senátě složeném z předsedy", PanelType.PANEL),
        ("Nejvyšší soud České republiky rozhodl takto:", PanelType.PANEL),
        ("Senát postoupil věc velkému senátu občanskoprávního kolegia", PanelType.PANEL),
        ("Věc byla předložena velkému senátu podle § 20 ZSS", PanelType.PANEL),
    ],
)
def test_ns_panel_type_from_heading(heading, expected):
    assert nsoud.panel_type(heading=heading) is expected


def test_ns_panel_type_is_unknown_only_without_input():
    assert nsoud.panel_type() is PanelType.UNKNOWN


# ------------------------------------------------------------------------------- US


@pytest.mark.parametrize(
    ("registry_sign", "expected"),
    [
        # Both signs are taken verbatim from extract/patterns.toml; nothing is invented.
        ("Pl. ÚS 33/2000", PanelType.PLENARY),
        ("Pl.ÚS 33/2000", PanelType.PLENARY),
        ("II. ÚS 2379/08", PanelType.PANEL),
        ("II.ÚS 2379/08", PanelType.PANEL),
    ],
)
def test_us_panel_type_from_registry_sign(registry_sign, expected):
    assert nalus.panel_type(registry_sign=registry_sign) is expected


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Plénum Ústavního soudu rozhodlo takto:", PanelType.PLENARY),
        ("Nález pléna Ústavního soudu ze dne", PanelType.PLENARY),
        ("plenum Ustavniho soudu rozhodlo", PanelType.PLENARY),
        ("Ústavní soud rozhodl v senátu složeném z předsedy senátu", PanelType.PANEL),
        # A panel referring the question to the plenum is still a panel.
        ("Senát postoupil věc plénu Ústavního soudu", PanelType.PANEL),
    ],
)
def test_us_panel_type_from_heading(heading, expected):
    assert nalus.panel_type(heading=heading) is expected


def test_us_registry_sign_wins_over_a_body_style_heading():
    """The sign is metadata; a heading that merely mentions the plenum must not override it."""
    assert (
        nalus.panel_type(
            registry_sign="II.ÚS 2379/08",
            heading="Ústavní soud rozhodl v senátu složeném z předsedy senátu",
        )
        is PanelType.PANEL
    )


def test_us_panel_type_is_unknown_only_without_input():
    assert nalus.panel_type() is PanelType.UNKNOWN


# ------------------------------------------------------- NALUS document-key derivation


def test_registry_sign_to_sz_matches_the_live_gettext_key():
    """Verified against nalus.usoud.cz: GetText.aspx?sz=2-2379-08_1 renders this decision."""
    assert nalus.registry_sign_to_sz("II. ÚS 2379/08") == "2-2379-08"
    assert nalus.registry_sign_to_sz("II.ÚS 2379/08") == "2-2379-08"
    assert nalus.text_url("2-2379-08").endswith("GetText.aspx?sz=2-2379-08_1")


def test_registry_sign_to_sz_refuses_to_guess_the_plenary_form():
    with pytest.raises(ValueError, match="never"):
        nalus.registry_sign_to_sz("Pl. ÚS 33/2000")

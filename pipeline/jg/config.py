"""Paths and environment. Shared contract between the crawl, extract and classify stages."""

from __future__ import annotations

import os
from pathlib import Path

# pipeline/jg/config.py -> pipeline/jg -> pipeline -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Gitignored. Raw HTML cache and e-Sbírka dumps. See CLAUDE.md rule 5.
DATA_DIR = Path(os.environ.get("JG_DATA_DIR", REPO_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
ESBIRKA_DIR = DATA_DIR / "esbirka"

#: Single source of the citation regexes, shared with the Java runtime. PLAN.md section 7.
PATTERNS_TOML = REPO_ROOT / "extract" / "patterns.toml"

#: Versioned prompt templates, read by both runtimes. PLAN.md section 3 (D7).
PROMPTS_DIR = REPO_ROOT / "prompts"

EVAL_DIR = REPO_ROOT / "eval"

#: The only hosts this project is permitted to fetch from. CLAUDE.md rule 6.
#: Adding a host here is a decision to escalate to the user, not a code change to make.
ALLOWLISTED_HOSTS = frozenset(
    {
        "nalus.usoud.cz",
        "www.nssoud.cz",
        "nssoud.cz",
        # Added on explicit user authorisation, 2026-09-02. The allowlisted www.nssoud.cz
        # carries no decision text at all — its sitemap covers only pages, people, career
        # and news — so NSS was uncrawlable without this host and milestone M1 was
        # unreachable. It serves no robots.txt (404), and it is a subdomain of the
        # already-allowlisted nssoud.cz. See PLAN.md section 18.
        "vyhledavac.nssoud.cz",
        "rozhodnuti.nsoud.cz",
        # Retired. Every path, including a nonsense control path, now returns one identical
        # notice that the service moved. Kept allowlisted only so the probe that proves this
        # can keep running from cache. See PLAN.md section 18.
        "opendata.eselpoint.cz",
        # Added on explicit user authorisation, 2026-09-03: the official successor to
        # opendata.eselpoint.cz, and the only remaining route to the statutory text and
        # validity dates that milestone M6 needs. Apex only — `www.e-sbirka.gov.cz` does
        # not resolve, so listing it would only produce confusing DNS failures.
        "e-sbirka.gov.cz",
    }
)

#: One request per second per host. CLAUDE.md rule 5.
REQUEST_DELAY_SECONDS = float(os.environ.get("JG_REQUEST_DELAY", "1.0"))

USER_AGENT = os.environ.get(
    "JG_USER_AGENT",
    "judikat-guard/0.1 (hackathon research prototype; contact via repository)",
)


def db_url() -> str:
    """libpq connection string. Matches the Makefile's JG_DB_URL default."""
    return os.environ.get(
        "JG_DB_URL", "postgresql://judikat:judikat@localhost:55432/judikat"
    )


def llm_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def llm_model() -> str:
    return os.environ.get("JG_LLM_MODEL", "claude-sonnet-5")


# --------------------------------------------------------------------- model providers
#
# Two providers, one seam. Anthropic is the original and is unchanged above; Gemini exists
# because neither a Claude Team plan nor a Google Pro subscription includes API access —
# both are seat products and the APIs are billed separately — while Google AI Studio does
# publish a genuinely free API tier. Which one a run uses is an environment decision, made
# here and nowhere else. Provider names are internal identifiers, so they stay English.

ANTHROPIC = "anthropic"
GEMINI = "gemini"

#: Google's cheapest high-throughput 3.5-line model, and the only place a Gemini model ID
#: is written down. Free-tier availability per model is undocumented and shifts, which is
#: exactly why ``JG_GEMINI_MODEL`` exists.
GEMINI_MODEL_DEFAULT = "gemini-3.5-flash-lite"

#: Seconds between two Gemini requests. Six is ten requests a minute, the low end of the
#: reported free-tier allowance. See :func:`gemini_min_interval`.
GEMINI_MIN_INTERVAL_DEFAULT = 6.0


def gemini_api_key() -> str | None:
    """``GEMINI_API_KEY``, then ``GOOGLE_API_KEY``. Both names are in circulation."""
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def gemini_model_name() -> str:
    return os.environ.get("JG_GEMINI_MODEL", GEMINI_MODEL_DEFAULT)


def gemini_min_interval() -> float:
    """Seconds to leave between two Gemini requests. ``JG_GEMINI_MIN_INTERVAL``, else 6.

    Six seconds is ten requests a minute, the low end of the reported free-tier allowance.
    Pacing inside the limit is cheaper than discovering it: a 429 costs a round trip and a
    backoff before any work happens. Set ``0`` on a paid tier, where it is lost throughput.
    An unparseable value falls back to the default rather than failing a batch over a typo.
    """
    raw = os.environ.get("JG_GEMINI_MIN_INTERVAL")
    if raw is None:
        return GEMINI_MIN_INTERVAL_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return GEMINI_MIN_INTERVAL_DEFAULT


def llm_provider() -> str:
    """Which provider a model call goes to: ``JG_LLM_PROVIDER``, else whichever key is set.

    An explicit setting always wins, including over a key for the other provider that
    happens to be exported. With neither key present this answers :data:`ANTHROPIC`, which
    keeps the no-key path exactly where it was: the caller finds no key, says so in Czech
    and stops. Never raises — an unrecognised name is rejected at the point a call would be
    built (:func:`jg.gemini.provider_model`), where the error can be reported as a sentence.
    """
    explicit = os.environ.get("JG_LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if gemini_api_key():
        return GEMINI
    return ANTHROPIC


def provider_api_key(provider: str | None = None) -> str | None:
    """The API key for the active provider, or ``None`` when it is not set."""
    return gemini_api_key() if (provider or llm_provider()) == GEMINI else llm_api_key()


def llm_model_name(provider: str | None = None) -> str:
    """The model ID the active provider will use — and the string persisted on every row.

    CLAUDE.md rule 7 requires each persisted row to record its ``model``. With two providers
    in play that string is also what makes a row's provenance unambiguous in a corpus
    holding rows from both, so it must be the real model ID, never the provider name.
    """
    return gemini_model_name() if (provider or llm_provider()) == GEMINI else llm_model()

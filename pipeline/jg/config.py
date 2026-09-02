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

"""Runtime settings loaded from the environment (and a local .env file).

Each setting is declared once, as a dataclass field with its default and a comment; the
``from_env`` readers take the default from the field and only add the allowed range.
Prices are not in the code: the LLM's token prices and Serper's credit price come from
the environment (``.env.example`` is prefilled with current list prices and their date),
and Exa reports its own cost. A missing price makes that cost "unknown" rather than a guess.
"""

from __future__ import annotations

import os
from dataclasses import MISSING, dataclass, field, fields

from dotenv import load_dotenv

# Ranking weights (ARCHITECTURE.md §4.10), the same for every brief: what "relevance" means
# comes from the brief's topics and preferences, which the LLM scores against.
DEFAULT_RANK_WEIGHTS = {
    "role_fit": 0.25, "relevance": 0.20, "evidence_strength": 0.25,
    "recency": 0.15, "corroboration": 0.10, "seniority": 0.05,
}
EXA_SEARCH_TYPES = ("instant", "fast", "auto", "deep-lite", "deep", "deep-reasoning")
_FALSE = {"0", "false", "no"}


@dataclass(frozen=True)
class LLMPricing:
    input_per_mtok: float | None  # USD per million input tokens
    output_per_mtok: float | None  # USD per million output tokens
    source: str  # where the prices came from, recorded in run_metadata.json

    @classmethod
    def from_env(cls) -> LLMPricing:
        price_in, price_out = _float_env("LLM_PRICE_IN_PER_MTOK"), _float_env("LLM_PRICE_OUT_PER_MTOK")
        if price_in is None or price_out is None:
            return cls(None, None, source="unknown: set LLM_PRICE_IN_PER_MTOK and LLM_PRICE_OUT_PER_MTOK")
        as_of = os.getenv("LLM_PRICES_AS_OF")
        return cls(price_in, price_out, source="LLM_PRICE_*_PER_MTOK" + (f" (as of {as_of})" if as_of else ""))


@dataclass(frozen=True)
class SearchSettings:
    serper_api_key: str | None = None
    exa_api_key: str | None = None
    # Serper (Google): 10 results per page and 1 credit per page; ``num`` > 10 is
    # refused on free accounts. Filtered LinkedIn X-rays run dry after 1-2 pages,
    # but site: queries over a vendor's case-study section page normally (50+).
    serper_max_pages: int = 10
    # Pages per LinkedIn X-ray (people query). Google started returning 5-10 pages per
    # X-ray (2026-10-10, was 1-2), but extraction reads rank 1 of every query first and
    # stops at the group's target: on Tungsten it read up to rank ~15 for former
    # employees and rank 5 for competitors, so pages 4+ were ~190 unread credits.
    serper_people_max_pages: int = 3
    # USD per 1,000 Serper credits. Serper reports credits used but not money, so the
    # cost is credits x this price; None (not set) = cost unknown.
    serper_price_per_1k: float | None = None
    # Exa: one request per query (no pagination), up to 100 results.
    exa_num_results: int = 50
    # Exa search type per query type. Measured 2026-10-10 on the Tungsten former-
    # employee query (50 requested): "auto" returned 50 results in ~2s, "deep" at
    # most 27 in ~12s (deep + additionalQueries and deep-lite did no better). Both
    # surfaced only 4-6 genuine former employees per call, so people search uses
    # "auto" for volume; company discovery (research-style) uses "deep".
    exa_people_search_type: str = "auto"
    exa_company_search_type: str = "deep"
    # Page text kept per Exa result for later candidate extraction (0 = none).
    exa_text_max_chars: int = 4000
    max_workers_per_provider: int = 3

    @classmethod
    def from_env(cls) -> SearchSettings:
        d = _defaults(cls)
        return cls(
            serper_api_key=os.getenv("SERPER_API_KEY") or None,
            exa_api_key=os.getenv("EXA_API_KEY") or None,
            serper_max_pages=_int_env("SERPER_MAX_PAGES", d["serper_max_pages"], 1, 10),
            serper_people_max_pages=_int_env("SERPER_PEOPLE_MAX_PAGES", d["serper_people_max_pages"], 1, 10),
            serper_price_per_1k=_float_env("SERPER_PRICE_PER_1K"),
            exa_num_results=_int_env("EXA_NUM_RESULTS", d["exa_num_results"], 1, 100),
            exa_people_search_type=_choice_env("EXA_PEOPLE_SEARCH_TYPE", d["exa_people_search_type"],
                                               EXA_SEARCH_TYPES),
            exa_company_search_type=_choice_env("EXA_COMPANY_SEARCH_TYPE", d["exa_company_search_type"],
                                                EXA_SEARCH_TYPES),
            exa_text_max_chars=_int_env("EXA_TEXT_MAX_CHARS", d["exa_text_max_chars"], 0, 100_000),
        )


@dataclass(frozen=True)
class ExtractionSettings:
    # Company-discovery pages read by the LLM per group (best-ranked first). Reading
    # also stops once the group has ORG_PEOPLE_QUERIES_PER_GROUP organisations: on the
    # saved runs the first 100 pages held 84-91% of the organisations' matches that
    # later produced useful people.
    max_pages_per_group: int = 150
    # Profiles/pages enriched and read per step, between early-stop checks.
    batch_size: int = 50
    llm_workers: int = 8
    # Fetch missing page/profile content through Exa /contents (~$0.001 per page).
    enrich: bool = True

    @classmethod
    def from_env(cls) -> ExtractionSettings:
        d = _defaults(cls)
        return cls(
            max_pages_per_group=_int_env("EXTRACTION_MAX_PAGES_PER_GROUP", d["max_pages_per_group"], 0, 1000),
            batch_size=_int_env("EXTRACTION_BATCH_SIZE", d["batch_size"], 1, 100),
            llm_workers=_int_env("EXTRACTION_LLM_WORKERS", d["llm_workers"], 1, 32),
            enrich=_bool_env("EXTRACTION_ENRICH", d["enrich"]),
        )


@dataclass(frozen=True)
class Settings:
    llm_pricing: LLMPricing
    anthropic_api_key: str | None
    llm_model: str = "claude-haiku-5-5"
    llm_effort: str = "medium"  # low | medium | high | xhigh | max
    # Max Exa queries and company-discovery queries per expert group, per provider.
    queries_per_provider: int = 5
    # Max Google LinkedIn X-ray queries per expert group. Google returns ~10 profiles per
    # filtered X-ray, so former-employee recall needs more slices than other query kinds;
    # 20 covers a 31-month window's quarters (11) plus the main function slices.
    xray_queries_per_group: int = 20
    # Max organisations (one X-ray each) searched for people per discovery group, best-
    # evidenced first. ~2 relevant people per query on Tungsten.
    org_people_queries_per_group: int = 40
    # Searching stops for a group once it has this many *qualified* candidates: passed
    # verification and judged by the LLM to meet the group's hard requirements, the same
    # test the final list uses (20 per group, plus a margin for merges and borderline
    # verdicts). Counting date fit alone (the first version, target 40) stopped groups
    # whose brief requires a seniority ("leads") too early. 0 = no early stop.
    target_candidates_per_group: int = 25
    # Count qualified candidates (LLM group check) for the early stop; false = date fit.
    qualify_with_llm: bool = True
    # Final output: up to this many experts per group (the case study asks for 20).
    max_experts_per_group: int = 20
    # New LLM verdicts per group at ranking (best code-scored first); candidates with a
    # cached verdict are always ranked as well.
    rank_pool_per_group: int = 60
    rank_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RANK_WEIGHTS))
    # Points (of 100) taken off the score of an expert with no profile URL (named only on a
    # web page, and profile lookup found nothing), so experts with a profile rank first.
    no_profile_penalty: int = 25
    # Ambiguous identity pairs (two profiles, near names, shared employer) the LLM may judge.
    max_identity_llm_pairs: int = 40
    # People named on web pages (no profile) whose LinkedIn profile is looked up, per group (0 = off).
    profile_lookups_per_group: int = 60
    search: SearchSettings = field(default_factory=SearchSettings)
    extraction: ExtractionSettings = field(default_factory=ExtractionSettings)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        d = _defaults(cls)
        return cls(
            llm_pricing=LLMPricing.from_env(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            llm_model=os.getenv("LLM_MODEL") or d["llm_model"],
            llm_effort=os.getenv("LLM_EFFORT") or d["llm_effort"],
            queries_per_provider=_int_env("QUERIES_PER_PROVIDER", d["queries_per_provider"], 1, 20),
            xray_queries_per_group=_int_env("XRAY_QUERIES_PER_GROUP", d["xray_queries_per_group"], 1, 30),
            org_people_queries_per_group=_int_env(
                "ORG_PEOPLE_QUERIES_PER_GROUP", d["org_people_queries_per_group"], 0, 200),
            target_candidates_per_group=_int_env(
                "TARGET_CANDIDATES_PER_GROUP", d["target_candidates_per_group"], 0, 1000),
            qualify_with_llm=_bool_env("QUALIFY_WITH_LLM", d["qualify_with_llm"]),
            max_experts_per_group=_int_env("MAX_EXPERTS_PER_GROUP", d["max_experts_per_group"], 1, 200),
            rank_pool_per_group=_int_env("RANK_POOL_PER_GROUP", d["rank_pool_per_group"], 1, 500),
            rank_weights=_weights_env("RANK_WEIGHTS"),
            no_profile_penalty=_int_env("NO_PROFILE_PENALTY", d["no_profile_penalty"], 0, 100),
            max_identity_llm_pairs=_int_env("MAX_IDENTITY_LLM_PAIRS", d["max_identity_llm_pairs"], 0, 1000),
            profile_lookups_per_group=_int_env(
                "PROFILE_LOOKUPS_PER_GROUP", d["profile_lookups_per_group"], 0, 500),
            search=SearchSettings.from_env(),
            extraction=ExtractionSettings.from_env(),
        )


def _defaults(cls) -> dict:
    """Field name -> its declared default (fields without one are left out)."""
    out = {}
    for f in fields(cls):
        if f.default is not MISSING:
            out[f.name] = f.default
        elif f.default_factory is not MISSING:
            out[f.name] = f.default_factory()
    return out


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _float_env(name: str) -> float | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return default if not raw else raw not in _FALSE


def _weights_env(name: str) -> dict[str, float]:
    """``role_fit=0.3,recency=0.1``: overrides for the default ranking weights, renormalised to 1."""
    weights = dict(DEFAULT_RANK_WEIGHTS)
    raw = (os.getenv(name) or "").strip()
    for part in filter(None, (x.strip() for x in raw.split(","))):
        key, _, value = part.partition("=")
        if key.strip() not in weights:
            raise ValueError(f"{name}: unknown weight {key.strip()!r} (known: {', '.join(weights)})")
        weights[key.strip()] = float(value)
    total = sum(weights.values())
    if total <= 0:
        raise ValueError(f"{name}: weights must sum to more than 0")
    return {k: v / total for k, v in weights.items()}


def _choice_env(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = (os.getenv(name) or default).strip()
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}, got {value!r}")
    return value

"""Search results, normalised across providers (stage 3)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .common import SCHEMA_VERSION, Provider, QueryType, StopReason


class SearchHit(BaseModel):
    """One result returned by one query, normalised across providers.

    The same page found by several queries/providers appears once per query, so
    provenance is never lost; ``canonical_url`` is what later stages merge on.
    """

    hit_id: str  # f"{query_id}#{rank}"
    query_id: str
    group_id: str
    provider: Provider
    query_type: QueryType
    organisation: str | None
    rank: int  # 1-based position within this query's unique results
    page: int | None  # Serper result page (1-based); None for Exa
    title: str
    url: str
    canonical_url: str
    snippet: str | None  # search-result description / first highlight (not page content)
    extra_snippets: list[str] = Field(default_factory=list)
    text: str | None = None  # page text returned by the provider (Exa contents)
    published_date: str | None = None  # as reported by the provider
    author: str | None = None
    # Provider-structured data, e.g. Exa person entities with dated work history.
    entities: list[dict] = Field(default_factory=list)
    retrieved_at: datetime


class QueryRun(BaseModel):
    query_id: str
    group_id: str
    provider: Provider
    query_type: QueryType
    query: str
    requests: int
    results: int  # unique results kept
    filtered: int = 0  # results dropped as junk (non-profile URLs for people queries, provider artefacts)
    stop_reason: StopReason
    duration_s: float
    cost_usd: float | None  # exact for Exa (costDollars), estimated from credits for Serper
    errors: list[str] = Field(default_factory=list)


class SearchConfig(BaseModel):
    serper_max_pages: int
    serper_people_max_pages: int | None = None
    exa_num_results: int
    exa_text_max_chars: int
    exa_people_search_type: str = "auto"
    exa_company_search_type: str = "auto"


class SearchSummaryRow(BaseModel):
    group_id: str
    provider: Provider
    query_type: QueryType
    queries: int
    hits: int
    unique_urls: int
    linkedin_profiles: int
    filtered: int = 0  # junk results dropped (non-profile URLs for people queries, provider artefacts)
    errors: int


class SearchResults(BaseModel):
    schema_version: str = SCHEMA_VERSION
    brief_id: str
    source_sha256: str
    executed_at: datetime
    config: SearchConfig
    summary: list[SearchSummaryRow]
    runs: list[QueryRun]
    hits: list[SearchHit]

    def for_group(self, group_id: str, provider: Provider | None = None) -> list[SearchHit]:
        return [h for h in self.hits if h.group_id == group_id and (provider is None or h.provider == provider)]

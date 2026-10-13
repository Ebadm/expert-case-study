"""Search terms and query plans (stage 2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .common import SCHEMA_VERSION, DiscoveryMode, Provider, QueryType


class QueryDraft(BaseModel):
    query: str = Field(description="The query text exactly as it should be sent to the engine.")
    language: str = Field(description="ISO 639-1 code of the language the query is written in.")
    intent: str = Field(description="Short phrase: what kind of page / organisation this query should surface.")


class SearchTermsDraft(BaseModel):
    """LLM output: terms for the LinkedIn X-ray and Exa people queries."""

    role_keywords: list[str] = Field(
        description="Short, broad job-title words (1-2 words) people in this group use in profile headlines, e.g. 'product', 'partnerships', 'go-to-market'."
    )
    topic_keywords: list[str] = Field(
        description="Short terms (1-3 words) for the product area or domain, e.g. 'document capture'."
    )
    title_keywords: list[str] = Field(
        description="Only when the group's hard requirements name a seniority (lead, leader, head, director, "
                    "manager): words that mark that seniority in LinkedIn job titles, in each of the group's query "
                    "languages, e.g. 'head', 'director', 'manager', 'lead'. Empty otherwise."
    )


class CompanySearchDraft(SearchTermsDraft):
    """LLM output for groups whose organisations must be discovered first."""

    serper: list[QueryDraft] = Field(
        description="Keyword queries for Google (via Serper) that surface qualifying organisations."
    )
    exa: list[QueryDraft] = Field(description="Natural-language queries for Exa that surface qualifying organisations.")
    customer_story_urls: list[str] = Field(
        description=(
            "Only when the group is the target's customers: URL prefixes (no https://) on the target's own "
            "websites where it publishes customer case studies or success stories, including former names' "
            "sites, e.g. 'example.com/customers'. Empty when unknown or when the group is not its customers."
        )
    )


class SearchQuery(BaseModel):
    query_id: str
    group_id: str
    provider: Provider
    query_type: QueryType
    query: str
    language: str
    intent: str
    organisation: str | None = None  # people queries: the organisation searched
    # Google paging, set by code: LinkedIn X-rays fetch page 1 first, and later pages only
    # when their group is still short of its target (adaptive.deepen). None = the client's cap.
    first_page: int = 1
    max_pages: int | None = None


class GeneratorInfo(BaseModel):
    model: str
    effort: str
    generated_at: datetime


class GroupQueryPlan(BaseModel):
    group_id: str
    mode: DiscoveryMode
    # Kept so people queries for discovered organisations use the same slicing.
    role_keywords: list[str] = Field(default_factory=list)
    topic_keywords: list[str] = Field(default_factory=list)
    title_keywords: list[str] = Field(default_factory=list)  # seniority words, when the group requires one
    queries: list[SearchQuery] = Field(default_factory=list)


class QueryPlan(BaseModel):
    schema_version: str = SCHEMA_VERSION
    brief_id: str
    # Which spec the queries were generated from.
    source_sha256: str
    spec_parsed_at: datetime
    generator: GeneratorInfo
    queries_per_provider: int  # max Exa / company-discovery queries per group and provider
    xray_queries_per_group: int | None = None  # max Google LinkedIn X-rays per group
    groups: list[GroupQueryPlan]
    validation_warnings: list[str] = Field(default_factory=list)

    @property
    def queries(self) -> list[SearchQuery]:
        return [q for g in self.groups for q in g.queries]

    def group(self, group_id: str) -> GroupQueryPlan:
        for g in self.groups:
            if g.group_id == group_id:
                return g
        raise KeyError(group_id)

    def for_group(
        self, group_id: str, provider: Provider | None = None, query_type: QueryType | None = None
    ) -> list[SearchQuery]:
        return [
            q for q in self.group(group_id).queries
            if (provider is None or q.provider == provider)
            and (query_type is None or q.query_type == query_type)
        ]

"""Run bookkeeping: usage, timings, cost and the run metadata file.

``run_metadata.json`` opens with ``summary`` (readable: status, time, total cost, cost per
API and time and cost per stage) and ``costs`` (the same numbers, machine-readable), then
the raw records every figure is computed from (each LLM call and each search round).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from .common import Provider


class SearchUsage(BaseModel):
    provider: Provider
    # "search" | "discovery" (customer-story site search) | "contents" (Exa page/profile
    # enrichment) | "profile_lookup" (finding the profile of a person named on a page)
    purpose: str = "search"
    requests: int = 0
    results: int = 0
    errors: int = 0
    cost_usd: float | None = 0.0
    cost_source: str = ""  # "provider-reported" | "estimate (...)" | "unknown"
    stage: str | None = None  # the pipeline stage that ran it (set by RunTracker)


class LLMCall(BaseModel):
    stage: str  # what the call was for, e.g. "candidate_extraction"
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    duration_s: float
    estimated_cost_usd: float | None
    pipeline_stage: str | None = None  # the pipeline stage it ran in (set by RunTracker)


class StageTiming(BaseModel):
    stage: str
    duration_s: float
    step: str | None = None  # position in the pipeline, e.g. "4/9"


class ApiCost(BaseModel):
    """One API's usage and cost in a run."""

    api: str  # "Anthropic", "Serper", "Exa"
    cost_usd: float | None  # None when part of it is unknown
    requests: int  # LLM calls, or HTTP requests
    by_purpose: dict[str, float | None] = Field(default_factory=dict)  # e.g. {"search": 0.2, "profile_lookup": 0.05}
    usage: dict[str, int | str] = Field(default_factory=dict)  # tokens, results, failures, model
    cost_source: str = ""


class StageCost(BaseModel):
    stage: str
    step: str | None = None
    duration_s: float
    cost_usd: float | None
    by_api: dict[str, float | None] = Field(default_factory=dict)
    llm_calls: int = 0


class CostBreakdown(BaseModel):
    total_usd: float | None
    duration_s: float | None
    by_api: list[ApiCost]
    by_stage: list[StageCost]


class RunSummary(BaseModel):
    """Readable overview at the top of run_metadata.json."""

    status: str  # "completed (stopped after ranking)" | "failed: ..."
    stages: str  # "candidate_extraction -> ranking"
    started: str
    duration: str
    total_cost: str  # "$2.33 (Anthropic $0.33 + Serper $0.25 + Exa $1.76)"
    cost_by_api: dict[str, str]
    time_and_cost_by_stage: dict[str, str]
    notes: list[str] = Field(default_factory=list)


class RunMetadata(BaseModel):
    summary: RunSummary | None = None
    costs: CostBreakdown | None = None
    run_id: str
    brief_id: str
    run_date: date
    started_at: datetime
    finished_at: datetime | None = None
    duration_s: float | None = None
    stopped_after: str | None = None
    stages: list[StageTiming] = Field(default_factory=list)
    llm_calls: list[LLMCall] = Field(default_factory=list)
    llm_total_input_tokens: int = 0
    llm_total_output_tokens: int = 0
    estimated_llm_cost_usd: float | None = None
    search_usage: list[SearchUsage] = Field(default_factory=list)
    estimated_search_cost_usd: float | None = None
    estimated_total_cost_usd: float | None = None
    pricing: dict[str, str | float | None] = Field(default_factory=dict)
    # Per-stage counters, e.g. {"resolution": {"merged_records": 12}, "verification": {...}}.
    counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

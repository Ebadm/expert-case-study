"""Time and cost summary at the top of run_metadata.json."""

from __future__ import annotations

import json
from datetime import date

from sourcing_agent.config import LLMPricing
from sourcing_agent.metrics import RunTracker
from sourcing_agent.models import SearchUsage

PRICING = LLMPricing(1.0, 5.0, "test prices")


def _run() -> RunTracker:
    t = RunTracker("brief", date(2026, 10, 10), PRICING)
    with t.stage("search", "3/9"):
        t.record_search_usage(SearchUsage(provider="serper", requests=40, results=380, cost_usd=0.04,
                                          cost_source="estimate (credits x $1.0/1k)"))
        t.record_search_usage(SearchUsage(provider="exa", requests=5, results=250, cost_usd=0.235,
                                          cost_source="provider-reported"))
    with t.stage("candidate_extraction", "4/9"):
        t.record_llm_call(stage="candidate_extraction", model="m", input_tokens=1_000_000, output_tokens=100_000,
                          duration_s=1.0)
        t.record_search_usage(SearchUsage(provider="exa", purpose="contents", requests=3, results=120, errors=4,
                                          cost_usd=0.12, cost_source="provider-reported"))
    with t.stage("profile_lookup", "6/9"):
        t.record_search_usage(SearchUsage(provider="serper", purpose="profile_lookup", requests=10, results=12,
                                          cost_usd=0.01, cost_source="estimate (credits x $1.0/1k)"))
    return t


def test_costs_add_up_per_api_and_per_stage():
    meta = _run().finish(stopped_after="profile_lookup")
    apis = {a.api: a for a in meta.costs.by_api}
    assert list(apis) == ["Anthropic", "Serper", "Exa"]
    assert apis["Anthropic"].cost_usd == 1.5  # 1M x $1 + 0.1M x $5
    assert apis["Serper"].cost_usd == 0.05 and apis["Serper"].by_purpose == {"search": 0.04, "profile_lookup": 0.01}
    assert apis["Exa"].cost_usd == 0.355
    assert meta.costs.total_usd == round(1.5 + 0.05 + 0.355, 6) == meta.estimated_total_cost_usd
    stages = {s.stage: s for s in meta.costs.by_stage}
    assert stages["candidate_extraction"].by_api == {"Anthropic": 1.5, "Exa": 0.12}
    assert stages["search"].cost_usd == 0.275 and stages["search"].step == "3/9"
    assert sum(s.cost_usd for s in meta.costs.by_stage) == meta.costs.total_usd


def test_summary_is_readable_and_first_in_the_file():
    meta = _run().finish(stopped_after="profile_lookup")
    sm = meta.summary
    assert sm.status == "completed (stopped after profile_lookup)"
    assert sm.total_cost == "$1.91 (Anthropic $1.50 + Serper $0.050 + Exa $0.355)"
    assert sm.cost_by_api["Anthropic (m)"] == "$1.50: 1 call, 1,000,000 input + 100,000 output tokens"
    assert "page contents $0.120 (120 pages read, 4 failed)" in sm.cost_by_api["Exa (search and page contents)"]
    assert sm.time_and_cost_by_stage["[4/9] candidate_extraction"].endswith("$1.62 (Anthropic $1.50, Exa $0.120)")
    assert next(iter(json.loads(meta.model_dump_json()))) == "summary"


def test_unknown_cost_is_not_understated():
    t = RunTracker("brief", date(2026, 10, 10), LLMPricing(None, None, "unknown model"))
    with t.stage("ranking"):
        t.record_llm_call(stage="ranking", model="m", input_tokens=10, output_tokens=10, duration_s=0.1)
    meta = t.finish(stopped_after="ranking")
    assert meta.costs.total_usd is None and meta.summary.total_cost.startswith("$?")
    assert any("unknown" in n for n in meta.summary.notes)


def test_failed_run_says_so():
    t = _run()
    t.meta.errors.append("QuotaExhausted: Exa account is out of credits")
    assert t.finish().summary.status.startswith("failed: QuotaExhausted")

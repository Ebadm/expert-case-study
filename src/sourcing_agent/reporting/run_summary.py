"""The time and cost summary at the top of run_metadata.json.

Built from the raw records the tracker keeps (every LLM call, every search round), so the
summary always adds up to them:

- total time and cost, and cost per API: Anthropic (per model: calls, tokens), Serper
  (requests, by purpose: search, customer-story discovery, profile lookup) and Exa
  (search requests, page contents);
- time and cost per pipeline stage, split by API.

Costs are estimates where a provider does not report money (Anthropic: list prices x
tokens; Serper: credits x configured price); a part that is unknown makes its total
unknown ("$?") rather than understated.
"""

from __future__ import annotations

from collections import defaultdict

from ..models import ApiCost, CostBreakdown, LLMCall, RunMetadata, RunSummary, SearchUsage, StageCost
from .progress import duration, money

API_NAMES = {"serper": "Serper", "exa": "Exa"}
API_ORDER = ["Anthropic", "Serper", "Exa"]
PURPOSE_NAMES = {
    "search": "search",
    "discovery": "customer-story discovery",
    "contents": "page contents",
    "profile_lookup": "profile lookup",
}


def summarise(meta: RunMetadata) -> tuple[RunSummary, CostBreakdown]:
    by_api = [a for a in (_anthropic(meta.llm_calls, meta.pricing), *_search_apis(meta.search_usage)) if a]
    by_api.sort(key=lambda a: API_ORDER.index(a.api) if a.api in API_ORDER else len(API_ORDER))
    by_stage = _stages(meta)
    total = _sum([a.cost_usd for a in by_api]) if by_api else 0.0
    costs = CostBreakdown(total_usd=total, duration_s=meta.duration_s, by_api=by_api, by_stage=by_stage)

    status = (f"failed: {meta.errors[-1][:200]}" if meta.errors
              else f"completed (stopped after {meta.stopped_after})")
    stages = f"{meta.stages[0].stage} -> {meta.stages[-1].stage}" if meta.stages else "none"
    parts = " + ".join(f"{a.api} {money(a.cost_usd)}" for a in by_api)
    summary = RunSummary(
        status=status,
        stages=stages,
        started=f"{meta.started_at:%Y-%m-%d %H:%M:%S} UTC",
        duration=duration(meta.duration_s or 0.0),
        total_cost=money(total) + (f" ({parts})" if parts else ""),
        cost_by_api={_api_label(a): _api_text(a) for a in by_api},
        time_and_cost_by_stage={
            (f"[{s.step}] {s.stage}" if s.step else s.stage): _stage_text(s) for s in by_stage
        },
        notes=_notes(by_api),
    )
    return summary, costs


def _anthropic(calls: list[LLMCall], pricing: dict) -> ApiCost | None:
    if not calls:
        return None
    by_stage: dict[str, list[LLMCall]] = defaultdict(list)
    for c in calls:
        by_stage[c.stage].append(c)
    models = sorted({c.model for c in calls})
    return ApiCost(
        api="Anthropic", cost_usd=_sum([c.estimated_cost_usd for c in calls]), requests=len(calls),
        by_purpose={k: _sum([c.estimated_cost_usd for c in v]) for k, v in by_stage.items()},
        usage={
            "model": ", ".join(models),
            "input_tokens": sum(c.input_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
        },
        cost_source=f"estimate: tokens x list price ({pricing.get('source', 'unknown')})",
    )


def _search_apis(usage: list[SearchUsage]) -> list[ApiCost]:
    out = []
    for provider in dict.fromkeys(u.provider for u in usage):
        mine = [u for u in usage if u.provider == provider]
        purposes: dict[str, list[SearchUsage]] = defaultdict(list)
        for u in mine:
            purposes[u.purpose].append(u)
        detail: dict[str, int | str] = {}
        for purpose, us in purposes.items():
            detail[f"{purpose}_requests"] = sum(u.requests for u in us)
            detail[f"{purpose}_results"] = sum(u.results for u in us)
            detail[f"{purpose}_failed"] = sum(u.errors for u in us)
        out.append(ApiCost(
            api=API_NAMES.get(provider, provider), cost_usd=_sum([u.cost_usd for u in mine]),
            requests=sum(u.requests for u in mine),
            by_purpose={p: _sum([u.cost_usd for u in us]) for p, us in purposes.items()},
            usage=detail, cost_source="; ".join(dict.fromkeys(u.cost_source for u in mine if u.cost_source)),
        ))
    return out


def _stages(meta: RunMetadata) -> list[StageCost]:
    out = []
    for t in meta.stages:
        calls = [c for c in meta.llm_calls if c.pipeline_stage == t.stage]
        usage = [u for u in meta.search_usage if u.stage == t.stage]
        by_api: dict[str, float | None] = {}
        if calls:
            by_api["Anthropic"] = _sum([c.estimated_cost_usd for c in calls])
        for provider in dict.fromkeys(u.provider for u in usage):
            by_api[API_NAMES.get(provider, provider)] = _sum([u.cost_usd for u in usage if u.provider == provider])
        out.append(StageCost(
            stage=t.stage, step=t.step, duration_s=t.duration_s,
            cost_usd=_sum(list(by_api.values())) if by_api else 0.0, by_api=by_api, llm_calls=len(calls),
        ))
    return out


def _api_label(a: ApiCost) -> str:
    if a.api == "Anthropic":
        return f"Anthropic ({a.usage.get('model')})"
    return {"Serper": "Serper (Google search)", "Exa": "Exa (search and page contents)"}.get(a.api, a.api)


def _api_text(a: ApiCost) -> str:
    if a.api == "Anthropic":
        calls = f"{a.requests:,} call" + ("s" if a.requests != 1 else "")
        return (f"{money(a.cost_usd)}: {calls}, {a.usage['input_tokens']:,} input + "
                f"{a.usage['output_tokens']:,} output tokens")
    parts = []
    for purpose, cost in a.by_purpose.items():
        req = a.usage.get(f"{purpose}_requests", 0)
        res = a.usage.get(f"{purpose}_results", 0)
        failed = a.usage.get(f"{purpose}_failed", 0)
        if purpose == "contents":
            what = f"{res:,} pages read" + (f", {failed:,} failed" if failed else "")
        else:
            what = f"{req:,} requests" + (f", {failed:,} failed" if failed else "")
        parts.append(f"{PURPOSE_NAMES.get(purpose, purpose)} {money(cost)} ({what})")
    return f"{money(a.cost_usd)}: " + "; ".join(parts)


def _stage_text(s: StageCost) -> str:
    split = ", ".join(f"{api} {money(c)}" for api, c in s.by_api.items())
    return f"{duration(s.duration_s)}, {money(s.cost_usd)}" + (f" ({split})" if split else "")


def _notes(by_api: list[ApiCost]) -> list[str]:
    notes = [f"{a.api} cost: {a.cost_source}" for a in by_api if a.cost_source]
    notes += [f"{a.api}: part of the cost is unknown, so its total is shown as $?"
              for a in by_api if a.cost_usd is None]
    return notes


def _sum(values: list[float | None]) -> float | None:
    if any(v is None for v in values):
        return None
    return round(sum(values), 6)

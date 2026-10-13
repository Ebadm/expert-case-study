"""Run bookkeeping: stage timings, LLM token usage, search usage and estimated cost.

Every LLM call and search round is recorded with the pipeline stage it ran in, so
``finish`` can put a time and cost summary (per API and per stage) at the top of
run_metadata.json (``reporting.run_summary``).
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Iterator

from .config import LLMPricing
from .models import LLMCall, RunMetadata, SearchUsage, StageTiming
from .reporting.progress import duration, money, say
from .reporting.run_summary import summarise


class RunTracker:
    def __init__(self, brief_id: str, run_date: date, pricing: LLMPricing) -> None:
        self.pricing = pricing
        self._t0 = time.perf_counter()
        self.meta = RunMetadata(
            run_id=f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
            brief_id=brief_id,
            run_date=run_date,
            started_at=datetime.now(timezone.utc),
            pricing={
                "llm_input_usd_per_mtok": pricing.input_per_mtok,
                "llm_output_usd_per_mtok": pricing.output_per_mtok,
                "source": pricing.source,
            },
        )
        self._stage: str | None = None  # the stage running now (stages run one at a time)

    @contextmanager
    def stage(self, name: str, step: str = "") -> Iterator[None]:
        """Time a stage; log its start and its end with duration, LLM calls and cost."""
        label = f"[{step}] {name}" if step else name
        say("%s ...", label)
        start, calls, usage = time.perf_counter(), len(self.meta.llm_calls), len(self.meta.search_usage)
        self._stage = name
        ok = False
        try:
            yield
            ok = True
        finally:
            took = time.perf_counter() - start
            self._stage = None
            self.meta.stages.append(StageTiming(stage=name, duration_s=round(took, 3), step=step or None))
            new_calls, new_usage = self.meta.llm_calls[calls:], self.meta.search_usage[usage:]
            llm = _sum_known([c.estimated_cost_usd for c in new_calls]) if new_calls else 0.0
            search = _sum_known([u.cost_usd for u in new_usage]) if new_usage else 0.0
            say("%s %s in %s: %d LLM calls (%s), search %s; run total %s", label, "done" if ok else "FAILED",
                duration(took), len(new_calls), money(llm), money(search), money(self.cost_so_far()))

    def cost_so_far(self) -> float | None:
        llm = _sum_known([c.estimated_cost_usd for c in self.meta.llm_calls]) if self.meta.llm_calls else 0.0
        search = _sum_known([u.cost_usd for u in self.meta.search_usage]) if self.meta.search_usage else 0.0
        return None if llm is None or search is None else llm + search

    def record_llm_call(
        self,
        *,
        stage: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        duration_s: float,
    ) -> LLMCall:
        call = LLMCall(
            stage=stage,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            duration_s=round(duration_s, 3),
            estimated_cost_usd=self._cost(input_tokens, output_tokens),
            pipeline_stage=self._stage,
        )
        self.meta.llm_calls.append(call)
        return call

    def record_search_usage(self, usage: SearchUsage) -> None:
        if usage.stage is None:
            usage = usage.model_copy(update={"stage": self._stage})
        self.meta.search_usage.append(usage)

    def finish(self, stopped_after: str | None = None) -> RunMetadata:
        m = self.meta
        m.finished_at = datetime.now(timezone.utc)
        m.duration_s = round(time.perf_counter() - self._t0, 3)
        m.stopped_after = stopped_after
        m.llm_total_input_tokens = sum(c.input_tokens for c in m.llm_calls)
        m.llm_total_output_tokens = sum(c.output_tokens for c in m.llm_calls)
        m.estimated_llm_cost_usd = _sum_known([c.estimated_cost_usd for c in m.llm_calls])
        m.estimated_search_cost_usd = _sum_known([u.cost_usd for u in m.search_usage])
        parts = [x for x in (m.estimated_llm_cost_usd, m.estimated_search_cost_usd) if x is not None]
        known = (not m.llm_calls or m.estimated_llm_cost_usd is not None) and (
            not m.search_usage or m.estimated_search_cost_usd is not None
        )
        # None when any part is unknown: a partial sum would understate the run's cost.
        m.estimated_total_cost_usd = round(sum(parts), 6) if known else None
        m.summary, m.costs = summarise(m)
        return m

    def _cost(self, input_tokens: int, output_tokens: int) -> float | None:
        # Cache-token pricing is ignored for now: the pipeline does not use prompt caching.
        p = self.pricing
        if p.input_per_mtok is None or p.output_per_mtok is None:
            return None
        return round(
            (input_tokens * p.input_per_mtok + output_tokens * p.output_per_mtok) / 1_000_000, 6
        )


def _sum_known(values: list[float | None]) -> float | None:
    """Sum of costs, or None if there are none or any is unknown."""
    if not values or any(v is None for v in values):
        return None
    return round(sum(values), 6)

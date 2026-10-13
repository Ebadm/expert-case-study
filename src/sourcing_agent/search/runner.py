"""Search execution: ``QueryPlan`` -> ``SearchResults``.

Each provider runs its queries on a small thread pool (a Serper query's pages
are fetched in order, since each depends on the last), and both providers run
at the same time. A provider without an API key is skipped and
every query for it is recorded as an error, so a partial run is visible rather
than silent.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Protocol

from ..config import SearchSettings
from ..metrics import RunTracker
from ..models import (
    Provider,
    QueryPlan,
    QueryRun,
    SearchConfig,
    SearchHit,
    SearchQuery,
    SearchResults,
    SearchSummaryRow,
    SearchUsage,
)
from ..reporting.progress import report_search
from .base import ProviderUnusable
from .exa import ExaSearch
from .serper import SerperSearch

log = logging.getLogger(__name__)

STAGE = "search"


class SearchClient(Protocol):
    provider: Provider
    cost_source: str

    def search(self, q: SearchQuery) -> tuple[list[SearchHit], QueryRun]: ...


def run_searches(
    plan: QueryPlan,
    settings: SearchSettings,
    *,
    clients: dict[str, SearchClient] | None = None,
    tracker: RunTracker | None = None,
) -> SearchResults:
    own_clients = clients is None
    clients = clients if clients is not None else make_clients(settings)
    queries = plan.queries
    by_provider: dict[str, list[SearchQuery]] = defaultdict(list)
    for q in queries:
        by_provider[q.provider].append(q)

    try:
        with ThreadPoolExecutor(max_workers=2) as outer:
            futures = {
                provider: outer.submit(_run_provider, provider, qs, clients.get(provider), settings)
                for provider, qs in by_provider.items()
            }
            outcomes = {provider: f.result() for provider, f in futures.items()}
    finally:
        if own_clients:
            for c in clients.values():
                c.close()

    run_by_id = {r.query_id: r for _, runs in outcomes.values() for r in runs}
    hits_by_id: dict[str, list[SearchHit]] = defaultdict(list)
    for hits, _ in outcomes.values():
        for h in hits:
            hits_by_id[h.query_id].append(h)
    # Keep the plan's order so the output reads group by group.
    runs = [run_by_id[q.query_id] for q in queries if q.query_id in run_by_id]
    hits = [h for q in queries for h in hits_by_id.get(q.query_id, [])]

    if tracker is not None:
        for provider in by_provider:
            client = clients.get(provider)
            tracker.record_search_usage(_usage(provider, runs, client.cost_source if client else "unknown"))

    results = SearchResults(
        brief_id=plan.brief_id,
        source_sha256=plan.source_sha256,
        executed_at=datetime.now(timezone.utc),
        config=SearchConfig(
            serper_max_pages=settings.serper_max_pages,
            serper_people_max_pages=settings.serper_people_max_pages,
            exa_num_results=settings.exa_num_results,
            exa_text_max_chars=settings.exa_text_max_chars,
            exa_people_search_type=settings.exa_people_search_type,
            exa_company_search_type=settings.exa_company_search_type,
        ),
        summary=summarise(runs, hits),
        runs=runs,
        hits=hits,
    )
    report_search(results)
    return results


def make_clients(settings: SearchSettings) -> dict[str, SearchClient]:
    clients: dict[str, SearchClient] = {}
    if settings.serper_api_key:
        clients["serper"] = SerperSearch(
            settings.serper_api_key, max_pages=settings.serper_max_pages,
            people_max_pages=settings.serper_people_max_pages,
            price_per_1k=settings.serper_price_per_1k,
        )
    if settings.exa_api_key:
        clients["exa"] = ExaSearch(
            settings.exa_api_key, num_results=settings.exa_num_results,
            text_max_chars=settings.exa_text_max_chars,
            people_search_type=settings.exa_people_search_type,
            company_search_type=settings.exa_company_search_type,
        )
    return clients


def _run_provider(
    provider: str, queries: list[SearchQuery], client: SearchClient | None, settings: SearchSettings
) -> tuple[list[SearchHit], list[QueryRun]]:
    if client is None:
        log.warning("%s: no API key, skipping %d queries", provider, len(queries))
        return [], [_skipped_run(q, f"{provider.upper()}_API_KEY not set") for q in queries]

    def one(q: SearchQuery) -> tuple[list[SearchHit], QueryRun]:
        try:
            return client.search(q)
        except ProviderUnusable:
            raise  # out of credits: stop the run instead of writing degraded results
        except Exception as exc:  # one bad query must not sink the run
            log.exception("%s %s failed", provider, q.query_id)
            return [], _skipped_run(q, f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=settings.max_workers_per_provider) as pool:
        outcomes = list(pool.map(one, queries))
    return [h for hits, _ in outcomes for h in hits], [run for _, run in outcomes]


def _skipped_run(q: SearchQuery, error: str) -> QueryRun:
    return QueryRun(
        query_id=q.query_id, group_id=q.group_id, provider=q.provider, query_type=q.query_type,
        query=q.query, requests=0, results=0, stop_reason="error", duration_s=0.0,
        cost_usd=0.0, errors=[error],
    )


def _usage(provider: str, runs: list[QueryRun], cost_source: str) -> SearchUsage:
    mine = [r for r in runs if r.provider == provider]
    costs = [r.cost_usd for r in mine if r.requests]
    return SearchUsage(
        provider=provider,
        requests=sum(r.requests for r in mine),
        results=sum(r.results for r in mine),
        errors=sum(1 for r in mine if r.errors),
        cost_usd=round(sum(costs), 6) if all(c is not None for c in costs) else None,
        cost_source=cost_source,
    )


def summarise(runs: list[QueryRun], hits: list[SearchHit]) -> list[SearchSummaryRow]:
    keys = list(dict.fromkeys((r.group_id, r.provider, r.query_type) for r in runs))
    rows = []
    for gid, provider, qtype in keys:
        rs = [r for r in runs if (r.group_id, r.provider, r.query_type) == (gid, provider, qtype)]
        hs = [h for h in hits if (h.group_id, h.provider, h.query_type) == (gid, provider, qtype)]
        urls = {h.canonical_url for h in hs}
        rows.append(SearchSummaryRow(
            group_id=gid, provider=provider, query_type=qtype,
            queries=len(rs), hits=len(hs), unique_urls=len(urls),
            linkedin_profiles=sum(1 for u in urls if u.startswith("https://linkedin.com/in/")),
            filtered=sum(r.filtered for r in rs),
            errors=sum(1 for r in rs if r.errors),
        ))
    return rows

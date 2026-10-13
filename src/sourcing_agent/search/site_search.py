"""Google site search for discovery: query -> result URLs (first page).

Used by query generation to find where the target publishes customer stories.
"""

from __future__ import annotations

from ..config import Settings
from ..metrics import RunTracker
from ..models import (
    SearchQuery,
    SearchUsage,
)
from ..search.runner import SearchClient
from ..search.serper import SerperSearch


def make_site_search(settings: Settings, clients: dict[str, SearchClient] | None, tracker: RunTracker):
    """Query -> result URLs (first Google page), for finding the target's customer-story pages."""
    client = (clients or {}).get("serper")
    if client is None and settings.search.serper_api_key:
        client = SerperSearch(
            settings.search.serper_api_key, max_pages=1, price_per_1k=settings.search.serper_price_per_1k
        )
    if client is None:
        return None

    def search_urls(query: str) -> list[str]:
        hits, run = client.search(
            SearchQuery(
                query_id="discovery",
                group_id="discovery",
                provider="serper",
                query_type="company",
                query=query,
                language="en",
                intent="find where the target publishes customer stories",
            )
        )
        tracker.record_search_usage(
            SearchUsage(
                provider="serper",
                purpose="discovery",
                requests=run.requests,
                results=run.results,
                errors=1 if run.errors else 0,
                cost_usd=run.cost_usd,
                cost_source=client.cost_source,
            )
        )
        return [h.url for h in hits]

    return search_urls

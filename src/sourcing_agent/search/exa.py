"""Exa search adapter.

POST https://api.exa.ai/search, auth via x-api-key. One request per query: Exa
has no pagination, ``numResults`` goes up to 100 (``deep`` returned at most ~27
in tests). The search ``type`` is configured separately for people and company
queries. People queries use ``category: "people"``. That category (and ``company``) rejects published-date
filters and ``excludeDomains`` with a 400, so neither is sent. Company-discovery
queries send no category, because many aim at case studies and press releases
rather than company home pages.

Exa reports the exact cost of each request in ``costDollars``. Results that
are not needed are dropped and counted as ``filtered``: anything but a LinkedIn
profile for people queries, and Exa's own synthetic ``exa.ai/library/...``
pages or job ads for company queries.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from ..models import QueryRun, SearchHit, SearchQuery
from .base import SearchError, canonical_url, clean_text, keep_result, raise_if_unusable, request_with_retries

log = logging.getLogger(__name__)

EXA_URL = "https://api.exa.ai/search"
MAX_NUM_RESULTS = 100
HIGHLIGHT_MAX_CHARS = 600


class ExaSearch:
    provider = "exa"
    cost_source = "provider-reported (costDollars)"

    def __init__(
        self,
        api_key: str,
        *,
        num_results: int = 50,
        text_max_chars: int = 4000,
        people_search_type: str = "auto",
        company_search_type: str = "deep",
        client: httpx.Client | None = None,
    ) -> None:
        self.num_results = max(1, min(num_results, MAX_NUM_RESULTS))
        self.search_types = {"people": people_search_type, "company": company_search_type}
        self.text_max_chars = text_max_chars
        # Deep search took ~12s in tests; leave generous headroom.
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    def build_body(self, q: SearchQuery) -> dict:
        body: dict = {"query": q.query, "type": self.search_types[q.query_type], "numResults": self.num_results}
        if q.query_type == "people":
            body["category"] = "people"
        contents: dict = {"highlights": {"maxCharacters": HIGHLIGHT_MAX_CHARS}}
        if self.text_max_chars:
            contents["text"] = {"maxCharacters": self.text_max_chars}
        body["contents"] = contents
        return body

    def search(self, q: SearchQuery) -> tuple[list[SearchHit], QueryRun]:
        start = time.perf_counter()
        hits: list[SearchHit] = []
        errors: list[str] = []
        cost: float | None = None
        filtered = 0

        try:
            resp = request_with_retries(self._client, "POST", EXA_URL, headers=self._headers, json=self.build_body(q))
        except SearchError as exc:
            resp = None
            errors.append(str(exc))

        if resp is not None and resp.status_code != 200:
            raise_if_unusable("Exa", resp.status_code, resp.text)
            errors.append(f"HTTP {resp.status_code}: {resp.text[:300]}")
        elif resp is not None:
            data = resp.json()
            cost = (data.get("costDollars") or {}).get("total")
            now = datetime.now(timezone.utc)
            seen: set[str] = set()
            for r in data.get("results") or []:
                url = r.get("url")
                if not url:
                    continue
                if not keep_result(url, q.query_type):
                    filtered += 1
                    continue
                canon = canonical_url(url)
                if canon in seen:
                    continue
                seen.add(canon)
                rank = len(hits) + 1
                highlights = [h for h in (clean_text(x) for x in r.get("highlights") or []) if h]
                hits.append(SearchHit(
                    hit_id=f"{q.query_id}#{rank}",
                    query_id=q.query_id, group_id=q.group_id, provider="exa",
                    query_type=q.query_type, organisation=q.organisation,
                    rank=rank, page=None,
                    title=(r.get("title") or "").strip() or url,
                    url=url, canonical_url=canon,
                    snippet=highlights[0] if highlights else None,
                    extra_snippets=highlights[1:],
                    text=r.get("text") or None,
                    published_date=r.get("publishedDate"),
                    author=r.get("author") or None,
                    entities=r.get("entities") or [],
                    retrieved_at=now,
                ))

        run = QueryRun(
            query_id=q.query_id, group_id=q.group_id, provider="exa", query_type=q.query_type,
            query=q.query, requests=1, results=len(hits), filtered=filtered,
            stop_reason="error" if errors else "single_request",
            duration_s=round(time.perf_counter() - start, 3),
            cost_usd=cost, errors=errors,
        )
        log.info("exa %s: %d results, $%s", q.query_id, len(hits), cost)
        return hits, run

    def close(self) -> None:
        self._client.close()

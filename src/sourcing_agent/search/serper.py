"""Serper adapter (Google web search).

POST https://google.serper.dev/search, auth via X-API-KEY, body ``{"q", "page"}``.
Google returns 10 results per page, ``page`` is 1-based, and each page costs one
credit. ``num`` above 10 is refused on free accounts ("Query pattern not
allowed"), so depth comes from paging. Filtered LinkedIn X-rays ran dry after
1-6 pages in live tests (2026-10-10). Paging stops when a page is empty, holds
under half a page of results (Google's last page; a full page can
still hold 8-9 after Google's own de-duplication), only repeats URLs already seen, or the
page limit is reached.

A query can start at a later page and cap its own pages (``first_page``, ``max_pages``):
LinkedIn X-rays fetch page 1 first, and the later pages only when their group is still
short (``adaptive.deepen``). Ranks then continue from that page (page 2 starts at 11).

No country (``gl``) is sent: briefs have no country constraint. ``hl`` is sent
for non-English queries so Google matches the query language.

Serper reports the credits each request used but not money, so cost is an
estimate from a configured price per 1,000 credits (or unknown).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from ..models import QueryRun, SearchHit, SearchQuery, StopReason
from .base import (
    DEFAULT_TIMEOUT,
    SearchError,
    canonical_url,
    clean_text,
    keep_result,
    raise_if_unusable,
    request_with_retries,
)

log = logging.getLogger(__name__)

SERPER_URL = "https://google.serper.dev/search"
PAGE_SIZE = 10
MAX_PAGES = 10


class SerperSearch:
    provider = "serper"

    def __init__(
        self,
        api_key: str,
        *,
        max_pages: int = 5,
        people_max_pages: int | None = None,
        price_per_1k: float | None = None,  # USD per 1,000 credits (SERPER_PRICE_PER_1K); None = unknown
        client: httpx.Client | None = None,
    ) -> None:
        self.max_pages = max(1, min(max_pages, MAX_PAGES))
        # LinkedIn X-rays are read best-rank first and stop at the group's target, so
        # pages beyond the first few are rarely read (see SearchSettings).
        self.people_max_pages = max(1, min(people_max_pages or self.max_pages, self.max_pages))
        self.price_per_1k = price_per_1k
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    @property
    def cost_source(self) -> str:
        if self.price_per_1k is None:
            return "unknown (set SERPER_PRICE_PER_1K)"
        return f"estimate (Serper-reported credits x ${self.price_per_1k}/1k)"

    def build_body(self, q: SearchQuery, page: int) -> dict:
        body: dict = {"q": q.query, "page": page}
        if q.language and q.language != "en":
            body["hl"] = q.language
        return body

    def search(self, q: SearchQuery) -> tuple[list[SearchHit], QueryRun]:
        start = time.perf_counter()
        hits: list[SearchHit] = []
        seen: set[str] = set()
        errors: list[str] = []
        requests = credits = filtered = 0
        stop: StopReason = "max_pages"

        cap = self.people_max_pages if q.query_type == "people" else self.max_pages
        last = cap if q.max_pages is None else min(cap, q.first_page + q.max_pages - 1)
        for page in range(q.first_page, last + 1):
            requests += 1
            try:
                resp = request_with_retries(
                    self._client, "POST", SERPER_URL, headers=self._headers, json=self.build_body(q, page)
                )
            except SearchError as exc:
                errors.append(str(exc))
                stop = "error"
                break
            if resp.status_code != 200:
                raise_if_unusable("Serper", resp.status_code, resp.text)
                errors.append(f"HTTP {resp.status_code} on page {page}: {resp.text[:300]}")
                stop = "error"
                break

            data = resp.json()
            credits += int(data.get("credits") or 1)
            results = data.get("organic") or []
            if not results:
                stop = "empty_page"
                break

            now = datetime.now(timezone.utc)
            new = 0
            for r in results:
                url = r.get("link")
                if not url:
                    continue
                canon = canonical_url(url)
                if canon in seen:
                    continue
                seen.add(canon)
                new += 1
                if not keep_result(url, q.query_type):
                    filtered += 1
                    continue
                rank = (q.first_page - 1) * PAGE_SIZE + len(hits) + 1
                hits.append(SearchHit(
                    hit_id=f"{q.query_id}#{rank}",
                    query_id=q.query_id, group_id=q.group_id, provider="serper",
                    query_type=q.query_type, organisation=q.organisation,
                    rank=rank, page=page,
                    title=clean_text(r.get("title")) or url,
                    url=url, canonical_url=canon,
                    snippet=clean_text(r.get("snippet")),
                    published_date=r.get("date"),
                    retrieved_at=now,
                ))
            if new == 0:
                stop = "repeated_page"
                break
            if len(results) < PAGE_SIZE // 2:
                stop = "no_more_results"
                break

        run = QueryRun(
            query_id=q.query_id, group_id=q.group_id, provider="serper", query_type=q.query_type,
            query=q.query, requests=requests, results=len(hits), filtered=filtered, stop_reason=stop,
            duration_s=round(time.perf_counter() - start, 3),
            cost_usd=None if self.price_per_1k is None else round(credits * self.price_per_1k / 1000, 6),
            errors=errors,
        )
        log.info("serper %s: %d results, %d requests, %s", q.query_id, len(hits), requests, stop)
        return hits, run

    def close(self) -> None:
        self._client.close()

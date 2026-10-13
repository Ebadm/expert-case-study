"""Page / profile enrichment via Exa ``/contents``.

Google (via Serper) only returns a title and a snippet, and LinkedIn pages must
not be scraped (login wall, terms), so fetching the HTML ourselves and passing it
to an LLM is not an option for profiles. Exa's index holds the page text and,
for LinkedIn profiles, a structured person entity with dated work history that
code parses without an LLM. In a probe, all 15 search-only profiles came back
from Exa's cache in 0.5s for $0.015. Company pages get their text the same way
and go to Haiku (``pages``).

POST https://api.exa.ai/contents with up to 100 URLs per request; Exa reports
per-URL status and the exact cost.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from ..search.base import SearchError, canonical_url, raise_if_unusable, request_with_retries

log = logging.getLogger(__name__)

EXA_CONTENTS_URL = "https://api.exa.ai/contents"
MAX_URLS_PER_REQUEST = 100


@dataclass
class PageContent:
    url: str
    title: str | None
    text: str | None
    published_date: str | None
    entities: list[dict] = field(default_factory=list)


@dataclass
class ContentsResult:
    pages: dict[str, PageContent]  # keyed by canonical URL
    requested: int = 0
    failed: list[str] = field(default_factory=list)
    requests: int = 0
    cost_usd: float | None = 0.0
    errors: list[str] = field(default_factory=list)


class ExaContents:
    def __init__(self, api_key: str, *, text_max_chars: int = 4000, client: httpx.Client | None = None) -> None:
        self.text_max_chars = text_max_chars
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    def fetch(self, urls: list[str]) -> ContentsResult:
        unique = list(dict.fromkeys(urls))
        out = ContentsResult(pages={}, requested=len(unique))
        for i in range(0, len(unique), MAX_URLS_PER_REQUEST):
            batch = unique[i:i + MAX_URLS_PER_REQUEST]
            out.requests += 1
            body = {"urls": batch, "text": {"maxCharacters": self.text_max_chars}}
            try:
                resp = request_with_retries(self._client, "POST", EXA_CONTENTS_URL, headers=self._headers, json=body)
            except SearchError as exc:
                out.errors.append(str(exc))
                out.failed += batch
                continue
            if resp.status_code != 200:
                raise_if_unusable("Exa", resp.status_code, resp.text)
                out.errors.append(f"HTTP {resp.status_code}: {resp.text[:300]}")
                out.failed += batch
                continue
            try:
                data = resp.json()
            except ValueError:  # a 200 with an unreadable body: record it, don't sink extraction
                out.errors.append(f"unreadable response body: {resp.text[:200]}")
                out.failed += batch
                continue
            cost = (data.get("costDollars") or {}).get("total")
            out.cost_usd = None if cost is None or out.cost_usd is None else round(out.cost_usd + cost, 6)
            for r in data.get("results") or []:
                url = r.get("url") or r.get("id")
                if not url:
                    continue
                out.pages[canonical_url(url)] = PageContent(
                    url=url, title=r.get("title"), text=r.get("text") or None,
                    published_date=r.get("publishedDate"), entities=r.get("entities") or [],
                )
            got = set(out.pages)
            out.failed += [u for u in batch if canonical_url(u) not in got]
        log.info("exa contents: %d/%d pages, $%s", len(out.pages), out.requested, out.cost_usd)
        return out

    def close(self) -> None:
        self._client.close()

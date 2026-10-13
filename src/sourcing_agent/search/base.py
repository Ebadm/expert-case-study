"""Shared pieces for search provider adapters: HTTP retries, URL canonicalisation, text cleanup."""

from __future__ import annotations

import html
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class SearchError(RuntimeError):
    pass


class ProviderUnusable(RuntimeError):
    """A provider can't serve any request in this run. Fatal: every stage re-raises it instead of
    recording a per-item failure, because carrying on would write degraded results."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider


class QuotaExhausted(ProviderUnusable):
    """The account is out of credits (seen live: Serper "Not enough credits" mid-run, Exa HTTP 402
    during stage 5)."""

    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(provider, f"{provider} account is out of credits ({detail[:160]}). Top up, then re-run "
                                   "from the last saved artifact (e.g. --search-results or --organisations).")


class InvalidCredentials(ProviderUnusable):
    """The API key is missing or was rejected (HTTP 401/403)."""

    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(provider, f"{provider} rejected the API key ({detail[:160]}). Check "
                                   f"{provider.upper()}_API_KEY in .env, then re-run from the last saved artifact.")


def raise_if_unusable(provider: str, status_code: int, text: str) -> None:
    """Raise for responses no retry or next query will fix: out of credits (402, or a
    400/403/429 that says so) or a rejected key (401/403)."""
    low = text.lower()
    if status_code == 402 or (status_code in (400, 403, 429) and ("credit" in low or "quota" in low)):
        raise QuotaExhausted(provider, f"HTTP {status_code}: {text}")
    if status_code in (401, 403):
        raise InvalidCredentials(provider, f"HTTP {status_code}: {text}")


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_attempts: int = 3,
    backoff_s: float = 1.0,
    sleep=time.sleep,
    **kwargs,
) -> httpx.Response:
    """Retry timeouts, connection errors, 429 and 5xx with bounded exponential backoff.

    Other 4xx responses are returned immediately (they won't succeed on retry).
    """
    last_error: str = ""
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code not in RETRY_STATUS:
                return response
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt < max_attempts:
                retry_after = _retry_after(response)
                delay = retry_after if retry_after is not None else backoff_s * 2 ** (attempt - 1)
                log.warning("%s %s -> %s; retrying in %.1fs", method, url, response.status_code, delay)
                sleep(delay)
                continue
            return response
        if attempt < max_attempts:
            sleep(backoff_s * 2 ** (attempt - 1))
    raise SearchError(f"{method} {url} failed after {max_attempts} attempts: {last_error}")


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return min(float(value), 30.0) if value else None
    except ValueError:
        return None


_LINKEDIN_HOST = re.compile(r"^(?:[a-z]{2,3}\.)?linkedin\.com$")


def canonical_url(url: str) -> str:
    """Normalise a URL for duplicate detection (never used for fetching).

    Lower-cases the host, drops "www.", query strings and fragments, and trailing
    slashes. LinkedIn country subdomains (uk., pl., ...) map to linkedin.com and
    LinkedIn paths are lower-cased, since profile slugs are case-insensitive.
    Profile paths keep only ``/in/<slug>``: Google also indexes language
    variants such as ``de.linkedin.com/in/<slug>/en``.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    if _LINKEDIN_HOST.match(host):
        host = "linkedin.com"
        path = path.lower()
        m = _PROFILE_PATH.match(path)
        if m:
            path = m.group(0)
    return urlunsplit(("https", host, path, "", ""))


_PROFILE_PATH = re.compile(r"^/in/[^/]+")
# /in/dir/... and /pub/dir/... are LinkedIn's name-directory pages, not profiles.
_NOT_PROFILE_SLUGS = {"dir"}


def is_linkedin_profile(url: str) -> bool:
    parts = urlsplit(url.strip())
    if not _LINKEDIN_HOST.match((parts.hostname or "").lower().removeprefix("www.")):
        return False
    m = _PROFILE_PATH.match(parts.path.lower())
    return bool(m) and m.group(0).removeprefix("/in/") not in _NOT_PROFILE_SLUGS


# Hosts whose results are provider artefacts or job ads, not sources: Exa returns
# its own synthetic exa.ai/library/... "publication" and "places" pages.
JUNK_HOSTS = ("exa.ai", "indeed.com", "glassdoor.com", "glassdoor.co.uk", "ziprecruiter.com")
JUNK_PATH_PREFIXES = {"linkedin.com": ("/jobs",)}


def is_junk_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in JUNK_HOSTS):
        return True
    canon = urlsplit(canonical_url(url))
    return any(canon.path.startswith(prefix) for prefix in JUNK_PATH_PREFIXES.get(canon.hostname or "", ()))


def keep_result(url: str, query_type: str) -> bool:
    """People queries keep only LinkedIn profiles; company queries drop junk hosts."""
    if query_type == "people":
        return is_linkedin_profile(url)
    return not is_junk_url(url)


_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str | None:
    """Strip HTML tags (e.g. <strong> highlighting) and unescape entities."""
    if value is None:
        return None
    text = html.unescape(_TAG_RE.sub("", value))
    return " ".join(text.split()) or None

"""Company-discovery queries for groups without named organisations.

The LLM's queries are cleaned and checked (near-duplicates, languages, people-search
wording). For groups defined by the target (its customers), Google queries are templates
aimed at where customers are named: the target's own case-study section (found with a site
search), case-study aggregators, and case studies elsewhere.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from urllib.parse import urlsplit

from ..models import (
    BriefSpec,
    CompanySearchDraft,
    ExpertGroup,
    Organisation,
    Provider,
    QueryDraft,
    SearchQuery,
)
from .scope import target_if_group_is_relative_to_it

PROVIDERS: tuple[Provider, ...] = ("serper", "exa")
NEAR_DUPLICATE_JACCARD = 0.8


def build_company_queries(
    spec: BriefSpec, group: ExpertGroup, draft: CompanySearchDraft, n: int
) -> tuple[list[SearchQuery], list[str]]:
    gid = group.group_id
    warnings: list[str] = []
    queries: list[SearchQuery] = []

    target = target_if_group_is_relative_to_it(spec, group)
    for provider in PROVIDERS:
        if provider == "serper" and target:
            continue  # replaced by customer-story templates below
        kept: list[QueryDraft] = []
        kept_tokens: list[set[str]] = []
        for d in getattr(draft, provider):
            text = " ".join(d.query.split())
            if not text:
                continue
            tokens = _tokens(text)
            if any(_jaccard(tokens, t) >= NEAR_DUPLICATE_JACCARD for t in kept_tokens):
                warnings.append(f"{gid}/company/{provider}: dropped near-duplicate query {text!r}")
                continue
            kept.append(d.model_copy(update={"query": text, "language": d.language.strip().lower()}))
            kept_tokens.append(tokens)

        if len(kept) > n:
            warnings.append(f"{gid}/company/{provider}: model returned {len(kept)} usable queries, kept first {n}")
            kept = kept[:n]
        elif len(kept) < n:
            warnings.append(f"{gid}/company/{provider}: only {len(kept)} usable queries (wanted {n})")

        for i, d in enumerate(kept, start=1):
            if d.language not in group.query_languages:
                warnings.append(f"{gid}/company/{provider}: query in unexpected language {d.language!r}: {d.query!r}")
            queries.append(SearchQuery(
                query_id=f"{gid}.company.{provider}.{i:02d}", group_id=gid, provider=provider,
                query_type="company", query=d.query, language=d.language, intent=d.intent.strip(),
            ))

        langs = {q.language for q in queries if q.provider == provider}
        for lang in group.query_languages:
            if lang not in langs:
                warnings.append(f"{gid}/company/{provider}: no queries in group language {lang!r}")

    for sq in queries:
        hits = people_markers(sq.query)
        if hits:
            warnings.append(
                f"{sq.query_id}: looks like a people search ({', '.join(hits)}), not company discovery: {sq.query!r}"
            )

    if target:
        names = [target.name, *target.aliases]
        for sq in queries:
            if not any(nm.lower() in sq.query.lower() for nm in names):
                warnings.append(f"{sq.query_id}: does not name the target ({target.name}): {sq.query!r}")
        queries = customer_story_queries(gid, target, draft.customer_story_urls, n) + queries
        all_text = " ".join(q.query.lower() for q in queries)
        for nm in names:
            # Template site: queries name the target by its URL slug ("tungsten-automation").
            if nm.lower() not in all_text and nm.lower().replace(" ", "-") not in all_text:
                warnings.append(f"{gid}: no company query mentions {nm!r} (a name of {target.name})")

    return queries, warnings


_STORY_SEGMENT = re.compile(r"case|customer|success|stor(?:y|ies)|testimonial|reference", re.IGNORECASE)


def discover_customer_story_sites(target: Organisation, site_search: Callable[[str], list[str]]) -> list[str]:
    """Where the target publishes customer stories, e.g. "tungstenautomation.com/case-studies".

    One search per name for its case studies; among result URLs on the target's own
    domains (host contains the name with spaces removed: "tungstenautomation"),
    the most common first path segment that looks like a customer-story section.
    The model did not know these paths, and ``site:`` over the right one returned
    96 case-study pages for Tungsten versus 42 mixed pages for the whole domain.
    """
    compact = [re.sub(r"[^a-z0-9]", "", n.lower()) for n in [target.name, *target.aliases]]
    counts: Counter[str] = Counter()
    for name in [target.name, *target.aliases]:
        for url in site_search(f'"{name}" ("case studies" OR "customer stories" OR "success stories")'):
            parts = urlsplit(url)
            host = (parts.hostname or "").lower().removeprefix("www.")
            segment = next((s for s in parts.path.split("/") if s), "")
            if any(c and c in host.replace("-", "") for c in compact) and _STORY_SEGMENT.search(segment):
                counts[f"{host}/{segment}"] += 1
    return [prefix for prefix, _ in counts.most_common(2)]


# Business-to-business case-study aggregators with one listing per vendor; on
# Tungsten they held 256 (featuredcustomers) and dozens (casestudies.com) of
# named customer stories, often with the quoted customer person and title.
CASE_STUDY_AGGREGATORS = ("featuredcustomers.com/vendor/{slug}", "casestudies.com/company/{slug}")


def customer_story_queries(gid: str, target: Organisation, story_urls: list[str], n: int) -> list[SearchQuery]:
    """Google queries for pages that name the target's customers (at most ``n``).

    Unfiltered ``site:`` queries page through Google normally (50+ results for
    the target's case-study section), unlike LinkedIn X-rays.
    """
    names = '(' + " OR ".join(f'"{x}"' for x in [target.name, *target.aliases]) + ')'
    slug = re.sub(r"[^a-z0-9]+", "-", target.name.lower()).strip("-")
    prefixes = list(dict.fromkeys(
        re.sub(r"^(?:https?://)?(?:www\.)?", "", u.strip()).rstrip("/") for u in story_urls if u.strip()
    ))[:2]
    templates = [(f"site:{p}", f"customer stories on the target's site ({p})") for p in prefixes]
    templates += [(f"site:{a.format(slug=slug)}", "case-study aggregator listing for the target")
                  for a in CASE_STUDY_AGGREGATORS]
    # A Google press-release template ("selects" OR "deploys" on newswires) was dropped:
    # 31 pages, 0 customers on Tungsten (the vendor's own announcements); Exa's
    # press-release queries do find customer announcements.
    templates.append((f'{names} ("case study" OR "customer story" OR "success story")',
                      "customer stories elsewhere on the web"))
    return [
        SearchQuery(query_id=f"{gid}.company.serper.{i:02d}", group_id=gid, provider="serper",
                    query_type="company", query=q, language="en", intent=intent)
        for i, (q, intent) in enumerate(templates[:n], start=1)
    ]


# Words that mark a query as aimed at individuals. English plus the Polish forms
# seen in real output ("były/była/byli" = former, "pracownicy" = employees).
_PEOPLE_MARKER_RE = re.compile(
    r"linkedin\.com|\bformer\b|\bex-|\bpeople\b|\bstaff\b|\bprofiles?\b|\bbiograph\w*|\bbio\b"
    r"|\bspeakers?\b|\bby[łl][aiy]?\b|\bpracowni\w*",
    re.IGNORECASE,
)


def people_markers(query: str) -> list[str]:
    return list(dict.fromkeys(m.group(0).lower() for m in _PEOPLE_MARKER_RE.finditer(query)))


_OPERATOR_RE = re.compile(r"\bsite:\S+|\bOR\b|[\"()]")


def _tokens(query: str) -> set[str]:
    text = _OPERATOR_RE.sub(" ", query).lower()
    return {t for t in re.split(r"[^\w]+", text) if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

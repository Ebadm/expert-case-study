"""Query generation: ``BriefSpec`` -> ``QueryPlan``.

Two query types, chosen per group. Caps per group: ``QUERIES_PER_PROVIDER``
(default 5) Exa queries and company-discovery queries, and
``XRAY_QUERIES_PER_GROUP`` (default 20) Google LinkedIn X-rays, because Google
returns only ~10 profiles per filtered X-ray and they cost ~$0.002 each.

* Organisations named in the brief (``mode="people"``): go straight to people
  search. Queries are built from templates (``people_plan`` with ``people_queries``):
  - Google (via Serper) LinkedIn X-rays, all excluding people currently at the
    organisation. Former employees: the name plus one departure quarter
    ("Jan 2025" OR "Feb 2025" OR "Mar 2025") or one job-function group per
    query, plus the former name (alias) with the window's earliest years or
    with employee titles. Current or current-or-former staff: the
    organisation + broad role words, then (if former staff qualify) people who
    mention the organisation without it in their title.
  - Exa people queries: one per organisation, plus a "moved on" phrasing for
    former employees.
  With several organisations, slices are taken round-robin (every
  organisation's main query first); with more organisations than the cap, they
  share queries. The LLM only supplies the terms: role and topic words.
* No named organisations (``mode="companies_then_people"``): an LLM writes N
  Exa (and Google) company-discovery queries from the brief. When the group is
  the target's customers, the Google queries are templates instead, aimed at
  where customers are actually named: the target's own case-study section
  (found with one search per name), case-study aggregators, and case studies
  elsewhere. People at the
  organisations found are searched in a later stage
  (``people_plan.build_organisation_people_queries``); company queries are cleaned and
  checked in ``company_queries``.

Runs as its own stage on a saved spec, so queries can be regenerated without
re-parsing the brief (which could silently change eligibility rules).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from ..llm import StructuredLLM
from ..models import (
    BriefSpec,
    CompanySearchDraft,
    ExpertGroup,
    GeneratorInfo,
    GroupQueryPlan,
    QueryPlan,
    SearchTermsDraft,
)
from .company_queries import build_company_queries, discover_customer_story_sites
from .people_plan import build_people_queries
from .scope import target_if_group_is_relative_to_it

STAGE = "query_generation"
MAX_ROLE_KEYWORDS = 6
MAX_TOPIC_KEYWORDS = 4
MAX_TITLE_KEYWORDS = 6

_KEYWORD_RULES = """\
Code builds LinkedIn X-ray queries for Google from these terms, for example \
site:linkedin.com/in "<organisation>" ("<role>" OR ...), plus natural-language Exa \
people queries.
- role_keywords: 3 to 6 short, broad headline words (1-2 words) covering the group's \
target roles, e.g. "product", "partnerships", "go-to-market", "solutions architect". \
Broad beats specific: "product" matches product manager, head of product and VP \
product. Leave out roles the group excludes. No organisation names.
- topic_keywords: 2 to 4 short terms (1-3 words) for the product area or domain, \
e.g. "document capture". They may include the target company's or its products' \
names when the group is defined relative to the target (for example customers); \
otherwise no organisation names.
- title_keywords: only when the group's hard requirements name a seniority ("leads", \
"leaders", "heads", "directors"): 3 to 6 words that mark that seniority in job titles as \
people write them, in each of the group's query languages (for example "head", \
"director", "manager", "lead" and their local equivalents). Empty when the hard \
requirements name no seniority (preferences don't count).
- Include keywords in each of the group's query languages.
"""

TERMS_SYSTEM_PROMPT = f"""\
You supply search keywords for one expert group of an expert-network project. The \
organisations to search are already known.

{_KEYWORD_RULES}"""

COMPANY_SYSTEM_PROMPT = f"""\
You write web search queries that find ORGANISATIONS, not people, that qualify for \
one expert group of an expert-network project. The organisation names found are \
then used in a separate people search, so each query should surface pages that name \
qualifying organisations and show why they qualify: company lists and directories, \
industry rankings, market reports, press releases, customer case studies, company \
pages.

Never target individual people in these queries: no job titles as the subject, no \
words such as "former", "people", "staff", "profile" or "biography" (or their \
local-language equivalents), and no profile sites such as linkedin.com/in. The \
people search comes later and is built separately.

Google (keyword search engine, field "serper"):
- Short keyword queries, usually 3 to 10 words.
- Operators available: "exact phrase", site:domain, -term, OR, intitle:word. Use at \
most one or two.

Exa (semantic search engine):
- Describe the organisations or pages wanted in natural language, for example \
"companies that run a large invoice-processing shared services centre in Europe" or \
"press release announcing that a bank selected <product> for loan document \
processing". No operators.

Rules:
- Work from the group's eligible organisation types, required evidence, topics and \
search strategy.
- When the group is defined relative to the target company (for example its \
customers or partners), every query must name the target, and aim at public \
evidence of the relationship: customer case studies, press releases about selecting \
or deploying it, conference talks, award entries, partner directories. Cover the \
target's former names too.
- An organisation excluded only for its current employees can still be an eligible \
organisation for a group of its former employees.
- Give each query a distinct angle; no near-duplicates; make the Google and Exa \
lists complement each other.
- Write queries in the group's query languages, splitting them when there is more \
than one. Local-language queries should read the way a native speaker searches.
- Return exactly the number of Google and Exa queries requested.
- When the group is the target's customers, also give customer_story_urls: the \
URL prefixes (no https://) on the target's own websites where it publishes customer \
case studies or success stories, including sites of its former names, e.g. \
"example.com/customers". Only paths you are confident exist; empty otherwise.

{_KEYWORD_RULES}"""


def generate_queries(
    spec: BriefSpec,
    *,
    llm: StructuredLLM,
    n: int,
    generator: GeneratorInfo,
    xray_n: int,
    site_search: Callable[[str], list[str]] | None = None,
    max_workers: int = 4,
) -> QueryPlan:
    """``site_search`` (query -> result URLs) lets customer groups find where the target
    publishes customer stories; without it only the LLM's suggestions are used."""
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(spec.groups)))) as pool:
        results = list(pool.map(lambda g: plan_group(spec, g, llm, n, xray_n, site_search), spec.groups))

    return QueryPlan(
        brief_id=spec.brief_id,
        source_sha256=spec.source.sha256,
        spec_parsed_at=spec.parser.parsed_at,
        generator=generator,
        queries_per_provider=n,
        xray_queries_per_group=xray_n,
        groups=[plan for plan, _ in results],
        validation_warnings=[w for _, warnings in results for w in warnings],
    )


def plan_group(
    spec: BriefSpec, group: ExpertGroup, llm: StructuredLLM, n: int, xray_n: int,
    site_search: Callable[[str], list[str]] | None = None,
) -> tuple[GroupQueryPlan, list[str]]:
    warnings: list[str] = []
    if group.eligible_organisations:
        org_names = ", ".join(o.name for o in group.eligible_organisations)
        terms = llm.parse(
            stage=STAGE,
            system=TERMS_SYSTEM_PROMPT,
            user=build_user_prompt(
                spec, group,
                f"Supply role_keywords, topic_keywords and title_keywords for people at these organisations: "
                f"{org_names}.",
            ),
            output_model=SearchTermsDraft,
        )
        terms = clean_terms(terms, group, warnings)
        queries, people_warnings = build_people_queries(
            spec, group, group.eligible_organisations, terms, n, max_xray=xray_n,
        )
        warnings += people_warnings
        mode = "people"
    else:
        draft = llm.parse(
            stage=STAGE,
            system=COMPANY_SYSTEM_PROMPT,
            user=build_user_prompt(
                spec, group,
                f"Write {n} Google and {n} Exa queries that find qualifying organisations, and supply "
                "role_keywords, topic_keywords and title_keywords for the people search that follows.",
            ),
            output_model=CompanySearchDraft,
        )
        terms = clean_terms(draft, group, warnings)
        target = target_if_group_is_relative_to_it(spec, group)
        if target:
            found = discover_customer_story_sites(target, site_search) if site_search else []
            if not found and not draft.customer_story_urls:
                warnings.append(f"{group.group_id}: found no customer-story section on {target.name}'s own site")
            draft = draft.model_copy(update={"customer_story_urls": found + draft.customer_story_urls})
        queries, company_warnings = build_company_queries(spec, group, draft, n)
        warnings += company_warnings
        mode = "companies_then_people"

    plan = GroupQueryPlan(
        group_id=group.group_id, mode=mode, role_keywords=terms.role_keywords,
        topic_keywords=terms.topic_keywords, title_keywords=terms.title_keywords, queries=queries,
    )
    return plan, warnings


def build_user_prompt(spec: BriefSpec, group: ExpertGroup, task: str) -> str:
    project = spec.model_dump(
        mode="json",
        include={
            "title", "objective", "sector", "target_organisation",
            "excluded_organisations", "global_hard_constraints", "global_preferences",
        },
    )
    group_data = group.model_dump(mode="json", exclude={"screening_questions", "departure_dates"})
    return (
        f"{task}\n\n"
        f"<project>\n{json.dumps(project, indent=2, ensure_ascii=False)}\n</project>\n\n"
        f"<group>\n{json.dumps(group_data, indent=2, ensure_ascii=False)}\n</group>"
    )


def clean_terms(terms: SearchTermsDraft, group: ExpertGroup, warnings: list[str]) -> SearchTermsDraft:
    if group.eligible_organisations and not terms.role_keywords:
        warnings.append(f"{group.group_id}: model gave no role_keywords")
    return SearchTermsDraft(
        role_keywords=_clean_keywords(terms.role_keywords, MAX_ROLE_KEYWORDS),
        topic_keywords=_clean_keywords(terms.topic_keywords, MAX_TOPIC_KEYWORDS),
        title_keywords=_clean_keywords(terms.title_keywords, MAX_TITLE_KEYWORDS),
    )


def _clean_keywords(words: list[str], limit: int) -> list[str]:
    seen: dict[str, str] = {}
    for w in words:
        w = " ".join(w.replace('"', "").split())
        if w and w.lower() not in seen:
            seen[w.lower()] = w
    return list(seen.values())[:limit]



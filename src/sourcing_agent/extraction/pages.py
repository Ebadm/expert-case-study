"""Web pages (case studies, press releases, registers) -> people and organisations.

Used for company-discovery groups, whose search results are pages rather than
profiles. Haiku reads one page and lists (a) named people who could belong to
the group and (b) organisations the page shows qualify. Every item must carry a
verbatim quote; code checks it against the page text and drops what it can't
find, so nothing invented reaches the candidate list.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..brief.parser import quote_in_text, slugify
from ..llm import StructuredLLM
from ..models import (
    BriefSpec,
    Candidate,
    DiscoveredOrganisation,
    Evidence,
    ExpertGroup,
    Organisation,
    PageExtractionDraft,
    Position,
)
from .enrichment import PageContent
from .orgs import clean_org_name, is_placeholder_org, matching_organisations, org_key

STAGE = "candidate_extraction"

PAGE_SYSTEM_PROMPT = """\
You read one web page for an expert-network sourcing project and extract only what \
the page itself states.

people: named individuals who could belong to the expert group described below, \
because the page shows their role at a qualifying organisation or their direct \
involvement (for example a customer's manager quoted in a case study about \
deploying the product, or a terminal director quoted about a new rail service). \
Leave out people mentioned only in passing, authors with no stated relevant role, \
and staff of the vendor or target company when the group is its customers.

organisations: organisations the page shows qualify for the group's organisation \
types (for example a named customer running the product, or a licensed rail \
freight operator). Give each organisation's proper name exactly as the page writes \
it ("Aviva", "Henkel"), never a description ("a major European insurer", "a \
regional US bank"): skip organisations the page does not name. When the group is \
the target's customers, list only organisations that use the target's products; \
leave out the target itself, its partners, resellers, consultancies and \
competitors.

Rules:
- Never add names, titles or organisations that are not on the page.
- "quote" must be copied verbatim from the page; it is checked automatically and \
items with quotes that are not found are discarded.
- Return empty lists when nothing on the page qualifies.
"""


def build_page_prompt(spec: BriefSpec, group: ExpertGroup, page: PageContent) -> str:
    group_info = {
        "group": group.name,
        "description": group.description,
        "eligible_organisation_types": group.eligible_organisation_types,
        "required_evidence": group.required_evidence,
        "target_roles": group.target_roles,
        "target_organisation": spec.target_organisation.model_dump() if spec.target_organisation else None,
    }
    return (
        f"<expert_group>\n{json.dumps(group_info, indent=2, ensure_ascii=False)}\n</expert_group>\n\n"
        f"<page url=\"{page.url}\" title=\"{(page.title or '').replace(chr(34), '')}\">\n{page.text}\n</page>"
    )


def extract_from_page(
    spec: BriefSpec,
    group: ExpertGroup,
    organisations: list[Organisation],
    page: PageContent,
    llm: StructuredLLM,
    *,
    hit_ids: list[str],
    query_ids: list[str],
    providers: list[str],
) -> tuple[list[Candidate], list[DiscoveredOrganisation], list[str]]:
    draft: PageExtractionDraft = llm.parse(
        stage=STAGE, system=PAGE_SYSTEM_PROMPT, user=build_page_prompt(spec, group, page),
        output_model=PageExtractionDraft,
    )
    now = datetime.now(timezone.utc)
    warnings: list[str] = []
    people: list[Candidate] = []
    orgs: list[DiscoveredOrganisation] = []

    for p in draft.people:
        if not quote_in_text(p.quote, page.text or ""):
            warnings.append(f"{page.url}: dropped person {p.name!r}, quote not found on page")
            continue
        if not _name_on_page(p.name, page.text or ""):
            warnings.append(f"{page.url}: dropped person {p.name!r}, name not found on page")
            continue
        org = (p.organisation or "").strip()
        positions = [Position(
            title=p.title, organisation=org, matched_organisations=matching_organisations(org, organisations),
            source="llm_page",
        )] if org else []
        people.append(Candidate(
            candidate_id=f"{group.group_id}:page:{slugify(p.name)}-{slugify(org_key(org)) or 'unknown'}",
            group_id=group.group_id, name=p.name.strip(), profile_url=None, canonical_url=None,
            headline=p.title, positions=positions,
            evidence=[Evidence(
                source_url=page.url, source_type="web_page", provider="exa", retrieved_at=now,
                claim=p.relevance, supporting_text=p.quote, evidence_type="quote",
                supports=["identity", "role", "group_relevance"],
            )],
            extraction_method="llm_page", data_as_of=page.published_date,
            source_hit_ids=hit_ids, providers=providers, query_ids=query_ids,
            notes=["found on a web page; no profile yet"],
        ))

    for o in draft.organisations:
        if not quote_in_text(o.quote, page.text or ""):
            warnings.append(f"{page.url}: dropped organisation {o.name!r}, quote not found on page")
            continue
        # Seen live: "IM (infrastructure manager, not named on the page)" with a valid quote,
        # "A major European insurance company (unnamed)", "Regional US bank".
        name = clean_org_name(o.name)
        if is_placeholder_org(name):
            warnings.append(f"{page.url}: dropped organisation {o.name!r}, a description rather than a name")
            continue
        if not _name_on_page(name.split("(")[0], page.text or ""):
            warnings.append(f"{page.url}: dropped organisation {o.name!r}, name not found on page")
            continue
        if _not_a_discovery(spec, group, name):
            continue
        orgs.append(DiscoveredOrganisation(
            group_id=group.group_id, name=name, relationship=o.relationship,
            evidence=[Evidence(
                source_url=page.url, source_type="web_page", provider="exa", retrieved_at=now,
                claim=o.relationship, supporting_text=o.quote, evidence_type="quote",
                supports=["organisation_relationship"],
            )],
        ))
    return people, orgs, warnings


def _not_a_discovery(spec: BriefSpec, group: ExpertGroup, name: str) -> bool:
    """The target, a group's named organisation, or one excluded for current *and* former staff.

    Organisations excluded only for current employees stay discoverable: their former
    staff can be exactly who a group wants (e.g. former PKP PLK planners).
    """
    known = list(group.eligible_organisations)
    if spec.target_organisation:
        known.append(spec.target_organisation)
    known += [
        ex.organisation for ex in spec.excluded_organisations
        if ex.scope == "current_and_former_employees"
        and (not ex.applies_to_group_ids or group.group_id in ex.applies_to_group_ids)
    ]
    return bool(matching_organisations(name, known))


def _name_on_page(name: str, text: str) -> bool:
    """Every word of the name appears on the page (handles 'Dr.', middle initials, line breaks)."""
    words = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 1]
    page = text.lower()
    return bool(words) and all(re.search(rf"\b{re.escape(w)}\b", page) for w in words)

"""What the LLM sees about a candidate: the dossier, built by code.

Positions (relevant first), the employment status code verified, and up to
``MAX_EVIDENCE_ITEMS`` evidence items with ids the reason must cite. Evidence of group fit
(a page quoting the person, a profile line naming the target) comes before employment
records. Quotes shown to people are cleaned (markdown, phone numbers, emails).
"""

from __future__ import annotations

import re
from datetime import date

from ..models import (
    BriefSpec,
    Candidate,
    Evidence,
    ExpertGroup,
    Position,
    ResolvedSet,
)
from ..verification.employment import departure_status, relevant_positions

MAX_EVIDENCE_ITEMS = 6
MAX_POSITIONS = 8
MAX_ORG_EVIDENCE = 2
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def clean_quote(text: str | None) -> str | None:
    """Plain text for display: markdown links and headings removed, whitespace collapsed,
    phone numbers and email addresses redacted (personal contact data is not needed)."""
    if not text:
        return text
    text = _MD_LINK.sub(r"\1", text)
    text = re.sub(r"(^|\n)\s*#+\s*", r"\1", text).replace("**", "")
    text = _EMAIL.sub("[email removed]", text)
    text = _PHONE.sub(lambda m: "[phone removed]" if sum(ch.isdigit() for ch in m.group()) >= 9 else m.group(), text)
    return " ".join(text.split())


def evidence_items(c: Candidate) -> list[Evidence]:
    """The evidence a reason may cite: everything but the bare identity snippet. Evidence of
    group fit comes first (a page quoting the person, a profile line naming the target), then
    structured employment records. Otherwise a merged person with many dated positions lost the
    case-study quote that is their evidence of use (seen live after profile lookup: three
    Tungsten customers went from "meets" to "unclear")."""
    items = [e for e in c.evidence if not (e.evidence_type == "snippet" and "identity" in e.supports)]
    order = {"provider_structured": 0, "quote": 1, "snippet": 2, "inferred": 3}

    def key(e: Evidence) -> tuple[int, int]:
        return (0 if {"group_relevance", "target_mention"} & set(e.supports) else 1, order[e.evidence_type])
    return sorted(items, key=key)[:MAX_EVIDENCE_ITEMS]


def organisation_evidence(resolved: ResolvedSet) -> dict[tuple[str, str], list[Evidence]]:
    """Per (group, organisation): the quotes that show a discovered organisation qualifies."""
    return {(o.group_id, o.name): o.evidence[:MAX_ORG_EVIDENCE] for o in resolved.organisations if o.include}


def org_items(group: ExpertGroup, c: Candidate, relevant: set[str],
              org_ev: dict[tuple[str, str], list[Evidence]]) -> list[Evidence]:
    names = {n for p in relevant_positions(c, relevant) for n in p.matched_organisations}
    items = [e for n in sorted(names) for e in org_ev.get((group.group_id, n), [])]
    return list({(e.source_url, e.supporting_text): e for e in items}.values())[:MAX_ORG_EVIDENCE]


def dossier(spec: BriefSpec, group: ExpertGroup, c: Candidate, relevant: set[str], ref: str,
            org_ev: dict[tuple[str, str], list[Evidence]] | None = None) -> str:
    rel = {id(p) for p in relevant_positions(c, relevant)}
    positions = sorted(c.positions, key=lambda p: (id(p) not in rel, -(p.end or p.start or date.min).toordinal()))
    lines = [f'<candidate ref="{ref}">', f"name: {c.name}", f"headline: {c.headline or '-'}",
             f"location: {c.location or '-'}",
             f"status (verified by code): {status_text(group, c, relevant)}",
             f"source names the target: {'yes' if c.target_mentioned else 'no' if c.target_mentioned is False else '-'}",
             "positions:"]
    lines += [f"- {position_text(p)}{' [relevant]' if id(p) in rel else ''}" for p in positions[:MAX_POSITIONS]]
    lines.append("evidence:")
    for k, e in enumerate(evidence_items(c), start=1):
        quote = f' | "{clean_quote(e.supporting_text)[:300]}"' if e.supporting_text else ""
        lines.append(f"- {ref}.E{k} [{e.source_type}, {e.evidence_type}] {e.claim}{quote} | {e.source_url}")
    orgs = org_items(group, c, relevant, org_ev or {})
    if orgs:
        lines.append("organisation evidence (why their organisation qualifies for the group):")
        for k, e in enumerate(orgs, start=1):
            quote = f' | "{clean_quote(e.supporting_text)[:300]}"' if e.supporting_text else ""
            lines.append(f"- {ref}.O{k} {e.claim}{quote} | {e.source_url}")
    return "\n".join(lines + ["</candidate>"])


def best_position(group: ExpertGroup, c: Candidate, relevant: set[str]) -> Position | None:
    """The position that makes them relevant: the current one at a relevant organisation, else the latest."""
    rel = relevant_positions(c, relevant)
    if not rel:
        return None
    current = [p for p in rel if p.is_current]
    if current:
        return max(current, key=lambda p: p.start or date.min)
    return max(rel, key=lambda p: (p.end or date.min, p.start or date.min, bool(p.title)))


def status_text(group: ExpertGroup, c: Candidate, relevant: set[str]) -> str:
    status = departure_status(c, group, relevant)
    if status == "still_current":
        return "current"
    ends = [p.end for p in relevant_positions(c, relevant) if p.end]
    if ends:
        return f"left {max(ends):%b %Y}"
    return "dates not stated"


def position_text(p: Position) -> str:
    start = p.start.strftime("%b %Y") if p.start else "?"
    end = "present" if p.is_current else (p.end.strftime("%b %Y") if p.end else "?")
    return f"{p.title or '?'} @ {p.organisation} ({start} - {end})"

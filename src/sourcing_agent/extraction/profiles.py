"""People-search hits (one person profile) -> ``Candidate``.

Order of preference, most reliable first:

1. Exa person entity (``exa_entities``): structured work history with month-level
   dates, read by code. No LLM, so no invented employers or dates.
2. Profile text without an entity (``llm_profile_text``): Haiku extracts the
   positions, each with a verbatim quote checked against the text. Positions
   whose quote is not found are dropped.
3. Search snippet only (``snippet_only``): code parses the result title and any
   "Experience: X" fragment. Snippets support discovery, not eligibility, so
   current/former status stays unknown.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from ..brief.parser import quote_in_text
from ..llm import StructuredLLM
from ..models import (
    Candidate,
    Evidence,
    ExpertGroup,
    Organisation,
    Position,
    ProfileDraft,
    SearchHit,
)
from .enrichment import PageContent
from .orgs import matching_organisations

STAGE = "candidate_extraction"

PROFILE_SYSTEM_PROMPT = """\
You read one professional profile and list the person's positions exactly as the \
text states them. Do not infer, complete or correct anything: an employer, title \
or date that is not in the text must not appear in your output, and a missing date \
is null. Copy into "quote" the verbatim line or lines that state each position and \
its dates; quotes are checked against the text automatically.
"""


def candidate_from_profile(
    group: ExpertGroup,
    organisations: list[Organisation],
    hits: list[SearchHit],
    content: PageContent | None,
    llm: StructuredLLM | None = None,
) -> tuple[Candidate, list[str]]:
    """Build one candidate from every hit for the same profile URL (+ its page content)."""
    first = hits[0]
    warnings: list[str] = []
    now = datetime.now(timezone.utc)
    provider = "exa"  # page content always comes from Exa (search hit or /contents)
    person = _person_entity(content)

    if person is not None:
        method = "exa_entities"
        props = person.get("properties", {})
        name = props.get("name") or _name_from_title(first.title)
        positions = [_position_from_work(w, organisations) for w in props.get("workHistory") or []]
        positions = [p for p in positions if p is not None]
        location = props.get("location")
        headline = _headline_from_text(content.text, name) or _headline_from_title(first.title)
        evidence = [
            Evidence(
                source_url=content.url, source_type="profile", provider=provider, retrieved_at=now,
                claim=_position_claim(p), supporting_text=_line_mentioning(content.text, p.organisation),
                evidence_type="provider_structured", supports=["employment", "dates", "role"],
            )
            for p in positions if p.matched_organisations
        ]
    elif content is not None and content.text and llm is not None:
        method = "llm_profile_text"
        draft: ProfileDraft = llm.parse(
            stage=STAGE, system=PROFILE_SYSTEM_PROMPT,
            user=f"<profile url=\"{content.url}\">\n{content.text}\n</profile>", output_model=ProfileDraft,
        )
        name = draft.name.strip() or _name_from_title(first.title)
        headline, location = draft.headline, None
        positions, evidence = [], []
        for d in draft.positions:
            if not quote_in_text(d.quote, content.text):
                warnings.append(f"{first.canonical_url}: dropped position with unverifiable quote {d.quote[:80]!r}")
                continue
            p = Position(
                title=d.title, organisation=d.organisation.strip(),
                start=_parse_month(d.start), end=_parse_month(d.end),
                is_current=True if (d.end or "").strip().lower() == "present" else None,
                date_precision="month" if d.start or d.end else "unknown",
                matched_organisations=matching_organisations(d.organisation, organisations),
                source="llm_profile_text",
            )
            positions.append(p)
            if p.matched_organisations:
                evidence.append(Evidence(
                    source_url=content.url, source_type="profile", provider=provider, retrieved_at=now,
                    claim=_position_claim(p), supporting_text=d.quote, evidence_type="quote",
                    supports=["employment", "dates", "role"],
                ))
    else:
        method = "snippet_only"
        name = _name_from_title(first.title)
        headline, location = _headline_from_title(first.title), None
        positions, evidence = [], []
        for org in _experience_from_snippets(hits):
            positions.append(Position(
                title=None, organisation=org, matched_organisations=matching_organisations(org, organisations),
                source="snippet_only",
            ))
        for h in hits:
            if h.snippet:
                evidence.append(Evidence(
                    source_url=h.url, source_type="search_snippet", provider=h.provider, retrieved_at=h.retrieved_at,
                    claim=f"Search result for query {h.query_id}", supporting_text=h.snippet,
                    evidence_type="snippet", supports=["discovery"],
                ))
                break
        warnings.append(f"{first.canonical_url}: no page content; candidate rests on a search snippet")

    identity = Evidence(
        source_url=first.url, source_type="profile", provider=first.provider, retrieved_at=first.retrieved_at,
        claim=f"Public profile of {name}", supporting_text=first.title, evidence_type="snippet",
        supports=["identity"],
    )
    candidate = Candidate(
        candidate_id=f"{group.group_id}:{_profile_slug(first.canonical_url)}",
        group_id=group.group_id, name=name, profile_url=first.url, canonical_url=first.canonical_url,
        headline=headline, location=location, positions=positions, evidence=[identity, *evidence],
        extraction_method=method,
        data_as_of=(content.published_date if content else None) or first.published_date,
        source_hit_ids=[h.hit_id for h in hits],
        providers=list(dict.fromkeys(h.provider for h in hits)),
        query_ids=list(dict.fromkeys(h.query_id for h in hits)),
    )
    if not any(p.matched_organisations for p in positions):
        candidate.notes.append("no position matched an organisation named in the brief")
    return candidate, warnings


def content_from_hit(hit: SearchHit) -> PageContent | None:
    """Exa search hits already carry the page text and entities."""
    if not (hit.text or hit.entities):
        return None
    return PageContent(url=hit.url, title=hit.title, text=hit.text, published_date=hit.published_date,
                       entities=hit.entities)


def _person_entity(content: PageContent | None) -> dict | None:
    if content is None:
        return None
    for e in content.entities:
        if e.get("type") == "person" and (e.get("properties") or {}).get("workHistory") is not None:
            return e
    return None


def _position_from_work(work: dict, organisations: list[Organisation]) -> Position | None:
    company = ((work.get("company") or {}).get("name") or "").strip()
    if not company:
        return None
    dates = work.get("dates") or {}
    start, end = _parse_date(dates.get("from")), _parse_date(dates.get("to"))
    return Position(
        title=(work.get("title") or "").strip() or None,
        organisation=company,
        start=start, end=end,
        # An open-ended role in a structured record means current as of the profile snapshot.
        is_current=end is None if start is not None else None,
        date_precision="month" if start or end else "unknown",
        matched_organisations=matching_organisations(company, organisations),
        source="exa_entities",
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_month(value: str | None) -> date | None:
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", value or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return date(int(m.group(1)), int(m.group(2)), 1)


def _position_claim(p: Position) -> str:
    def fmt(d: date | None) -> str:
        return d.strftime("%b %Y") if d else "?"
    end = "present" if p.is_current else fmt(p.end)
    return f"{p.title or 'Role'} at {p.organisation} ({fmt(p.start)} - {end})"


_TITLE_SPLIT = re.compile(r"\s+[-–|]\s+")


def _name_from_title(title: str) -> str:
    return _TITLE_SPLIT.split(title.replace("| LinkedIn", "").strip())[0].strip()


def _headline_from_title(title: str) -> str | None:
    parts = _TITLE_SPLIT.split(title.replace("| LinkedIn", "").strip())
    rest = [p for p in parts[1:] if p and p != "Professional Profile"]
    return rest[0] if rest else None


def _headline_from_text(text: str | None, name: str) -> str | None:
    """Exa profile text starts '# Name' followed by the headline line."""
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and lines[0].lstrip("# ").strip() == name and len(lines) > 1 and not lines[1].startswith("#"):
        return lines[1][:200]
    return None


def _line_mentioning(text: str | None, organisation: str) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        if organisation.lower() in line.lower():
            return line.strip()[:300]
    return None


_EXPERIENCE_RE = re.compile(r"Experience:\s*([^·|]+?)\s*(?:·|\||$)")


def _experience_from_snippets(hits: list[SearchHit]) -> list[str]:
    found: list[str] = []
    for h in hits:
        for text in [h.snippet or "", *h.extra_snippets]:
            found += [m.group(1).strip() for m in _EXPERIENCE_RE.finditer(text)]
    return list(dict.fromkeys(f for f in found if f))


def _profile_slug(canonical: str) -> str:
    path = urlsplit(canonical).path.rstrip("/")
    if "/in/" in path:
        return path.split("/in/")[-1]
    return hashlib.sha1(canonical.encode()).hexdigest()[:12]

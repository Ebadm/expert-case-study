"""Fit score: LLM components (0-5) plus code components, weighted (ARCHITECTURE.md §4.10).

Code scores what it can measure: recency (current staff 1.0, else how recently they left
within the window) and corroboration (a structured record or page quote, plus further
independent sites). An expert with no profile URL loses ``no_profile_penalty`` points, so
people the client can open a profile for rank first.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urlsplit

from ..models import (
    BriefSpec,
    Candidate,
    CandidateAssessmentDraft,
    ExpertGroup,
    ScoreBreakdown,
)
from ..verification.employment import departure_status, relevant_positions
from .dossier import evidence_items


def score_breakdown(spec: BriefSpec, group: ExpertGroup, c: Candidate, relevant: set[str],
           d: CandidateAssessmentDraft, weights: dict[str, float],
           no_profile_penalty: float = 0.0) -> ScoreBreakdown:
    def unit(x: int) -> float:
        return round(max(0, min(5, x)) / 5, 3)
    parts = {
        "role_fit": unit(d.role_fit), "relevance": unit(d.relevance), "seniority": unit(d.seniority),
        "evidence_strength": unit(d.evidence_strength),
        "recency": recency_score(spec, group, c, relevant), "corroboration": corroboration_score(c),
    }
    penalty = 0.0 if c.profile_url else float(no_profile_penalty)
    total = round(max(0.0, 100 * sum(weights.get(k, 0) * v for k, v in parts.items()) - penalty), 1)
    return ScoreBreakdown(**parts, profile_penalty=penalty, total=total)


def recency_score(spec: BriefSpec, group: ExpertGroup, c: Candidate, relevant: set[str]) -> float:
    """1.0 for current staff; for leavers, how recently they left within the window (or 10 years)."""
    status = departure_status(c, group, relevant)
    if status == "still_current":
        return 1.0
    ends = [p.end for p in relevant_positions(c, relevant) if p.end]
    if not ends:
        return 0.4  # undated (allowed only where the group's rule doesn't need dates)
    months = _months(max(ends), spec.run_date)
    window = group.departure_dates
    span = _months(window.left_on_or_after, spec.run_date) if window and window.left_on_or_after else 120
    return round(max(0.1, min(1.0, 1 - months / max(span, 1))), 3)


def corroboration_score(c: Candidate) -> float:
    """Independent sources: a structured record 0.6 or a page quote 0.5; each extra site +0.2."""
    items = evidence_items(c)
    sites = {_site(e.source_url) for e in items if e.evidence_type != "snippet"}
    if not sites:
        return 0.2
    base = 0.6 if any(e.evidence_type == "provider_structured" for e in items) else 0.5
    return round(min(1.0, base + 0.2 * (len(sites) - 1)), 3)


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or url).lower()
    return ".".join(host.split(".")[-2:])


def _months(earlier: date, later: date) -> int:
    return (later.year - earlier.year) * 12 + later.month - earlier.month

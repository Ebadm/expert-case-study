"""Employment-date checks against a group's departure window (deterministic).

Dates from structured profiles are month-level ("2026-04" -> 2026-04-01), while
the window is day-exact (from the run date). Month-level departures are compared
in whole months, the way the brief ("at least 6 months", "last 3 years") and
LinkedIn both count: with a run date of 2026-10-10, leaving in April 2026 is
6 months ago and in October 2023 is 36 months ago, so both are inside the
window. ``boundary_month`` reports such cases so a reviewer can see them.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from ..models import BriefSpec, Candidate, ExpertGroup, Position

DepartureStatus = Literal[
    "left_within_window",
    "left_too_recently",
    "left_too_long_ago",
    "former_no_window",
    "still_current",
    "dates_unknown",
    "no_relevant_position",
]


def relevant_organisations(spec: BriefSpec, group: ExpertGroup, discovered: list[str] | None = None) -> set[str]:
    """The organisations a group's employment rule is about: its named ones, else the ones
    company discovery found for it (e.g. the target's customers), else the target."""
    names = {o.name for o in group.eligible_organisations} or set(discovered or [])
    if not names and spec.target_organisation:
        names.add(spec.target_organisation.name)
    return names


def useful_statuses(group: ExpertGroup) -> set[str]:
    """Statuses that fit the group's employment rule, i.e. the pool ranking picks from."""
    if group.required_employment_status == "current":
        return {"still_current"}
    statuses = {"left_within_window", "former_no_window"}
    if group.required_employment_status == "current_or_former":
        statuses.add("still_current")
    return statuses


def count_useful(candidates: list[Candidate], group: ExpertGroup, organisations: set[str]) -> int:
    """Candidates of ``group`` whose dates fit its rule (role fit is judged later, by ranking)."""
    ok = useful_statuses(group)
    return sum(1 for c in candidates
               if c.group_id == group.group_id and departure_status(c, group, organisations) in ok)


def relevant_positions(candidate: Candidate, organisations: set[str]) -> list[Position]:
    return [p for p in candidate.positions if set(p.matched_organisations) & organisations]


def departure_status(candidate: Candidate, group: ExpertGroup, organisations: set[str]) -> DepartureStatus:
    positions = relevant_positions(candidate, organisations)
    if not positions:
        return "no_relevant_position"
    if any(p.is_current for p in positions):
        return "still_current"
    last = _last_end(positions)
    if last is None:
        return "dates_unknown"
    window = group.departure_dates
    if window is None:
        return "former_no_window"
    end, monthly = last
    after, before = window.left_on_or_after, window.left_on_or_before
    if monthly:
        end, after, before = _month(end), after and _month(after), before and _month(before)
    if after and end < after:
        return "left_too_long_ago"
    if before and end > before:
        return "left_too_recently"
    return "left_within_window"


def boundary_month(candidate: Candidate, group: ExpertGroup, organisations: set[str]) -> date | None:
    """The departure month when a month-level departure falls in a window's boundary month, else None."""
    last = _last_end(relevant_positions(candidate, organisations))
    window = group.departure_dates
    if last is None or not last[1] or window is None:
        return None
    end = last[0]
    edges = [d for d in (window.left_on_or_after, window.left_on_or_before) if d]
    return end if any(_month(end) == _month(d) for d in edges) else None


def _last_end(positions: list[Position]) -> tuple[date, bool] | None:
    """The latest end date and whether it is month-level."""
    ended = [p for p in positions if p.end]
    if not ended:
        return None
    p = max(ended, key=lambda p: p.end)
    return p.end, p.date_precision == "month"


def _month(d: date) -> date:
    return d.replace(day=1)


def last_departure(candidate: Candidate, organisations: set[str]) -> date | None:
    ends = [p.end for p in relevant_positions(candidate, organisations) if p.end and not p.is_current]
    return max(ends) if ends else None

"""Human-review exports of a ``CandidateSet``: candidates.csv and organisations.csv.

candidates.csv has one row per candidate, sorted so the likely-eligible come
first, with the relevant role, current employer, departure status, best
evidence, and empty reviewer columns (ARCHITECTURE.md §4.11) so the sheet doubles as the
precision-labelling file. UTF-8 with BOM so Excel shows Polish characters.

Regenerate from saved results without any API calls:
    python -m sourcing_agent.reporting.review_sheets results/tungsten_automation
"""

from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

from ..models import BriefSpec, Candidate, CandidateSet, ExpertGroup
from ..verification.employment import (
    boundary_month,
    departure_status,
    last_departure,
    relevant_organisations,
    relevant_positions,
)

CANDIDATES_CSV = "candidates.csv"
_FAR = date(1900, 1, 1)
ORGANISATIONS_CSV = "organisations.csv"

REVIEW_COLUMNS = [
    "identity_correct", "role_relevant", "organisation_relationship_correct", "employment_dates_valid",
    "exclusions_honoured", "evidence_supports_claim", "independent_sources_sufficient", "overall_eligible",
    "reviewer_notes",
]
CANDIDATE_COLUMNS = [
    "group_id", "departure_status", "name", "profile_url", "headline", "location",
    "relevant_organisation", "relevant_title", "relevant_start", "relevant_end", "left_relevant_org",
    "current_employer", "current_title", "target_mentioned", "extraction_method", "data_as_of", "providers", "queries",
    "evidence_claim", "evidence_quote", "evidence_url", "notes", "candidate_id", *REVIEW_COLUMNS,
]

# Most promising first. Groups that allow current staff rank them with the eligible formers.
_ORDER_FORMER = [
    "left_within_window", "former_no_window", "dates_unknown",
    "left_too_recently", "left_too_long_ago", "still_current", "no_relevant_position",
]
_ORDER_CURRENT_OK = [
    "left_within_window", "still_current", "former_no_window", "dates_unknown",
    "left_too_long_ago", "left_too_recently", "no_relevant_position",
]


def candidate_rows(spec: BriefSpec, cs: CandidateSet) -> list[dict[str, str]]:
    rows: list[tuple[tuple, dict[str, str]]] = []
    for gi, group in enumerate(spec.groups):
        discovered = [o.name for o in cs.organisations if o.group_id == group.group_id and o.include]
        orgs = relevant_organisations(spec, group, discovered)
        order = _ORDER_FORMER if group.required_employment_status == "former" else _ORDER_CURRENT_OK
        for c in cs.for_group(group.group_id):
            status = departure_status(c, group, orgs) if orgs else "no_relevant_position"
            row = _row(c, group, orgs, status)
            left = last_departure(c, orgs)
            # Customer-type groups: people whose profile names the target first (hands-on evidence).
            evidence_rank = 0 if c.target_mentioned or group.eligible_organisations else 1
            rows.append(((gi, order.index(status), evidence_rank, -(left.toordinal() if left else 0), c.name.lower()),
                         row))
    return [r for _, r in sorted(rows, key=lambda x: x[0])]


def _row(c: Candidate, group: ExpertGroup, orgs: set[str], status: str) -> dict[str, str]:
    rel = sorted(relevant_positions(c, orgs), key=lambda p: (p.end or p.start or _FAR), reverse=True)
    best = rel[0] if rel else (c.positions[0] if c.positions else None)
    current = next((p for p in c.positions if p.is_current and not (set(p.matched_organisations) & orgs)), None)
    left = last_departure(c, orgs)
    ev = next((e for e in c.evidence if e.evidence_type != "snippet" or "identity" not in e.supports), None)
    notes = list(c.notes)
    edge = boundary_month(c, group, orgs)
    if edge:
        notes.insert(0, f"left in a boundary month ({edge:%b %Y}); counted in whole months, the exact day is unknown")
    return {
        "group_id": group.group_id,
        "departure_status": status,
        "name": c.name,
        "profile_url": c.profile_url or "",
        "headline": c.headline or "",
        "location": c.location or "",
        "relevant_organisation": best.organisation if best else "",
        "relevant_title": (best.title or "") if best else "",
        "relevant_start": _d(best.start) if best else "",
        "relevant_end": ("present" if best.is_current else _d(best.end)) if best else "",
        "left_relevant_org": _d(left),
        "current_employer": current.organisation if current else "",
        "current_title": (current.title or "") if current else "",
        "target_mentioned": {True: "yes", False: "no", None: ""}[c.target_mentioned],
        "extraction_method": c.extraction_method,
        "data_as_of": (c.data_as_of or "")[:10],
        "providers": ",".join(c.providers),
        "queries": str(len(c.query_ids)),
        "evidence_claim": ev.claim if ev else "",
        "evidence_quote": (ev.supporting_text or "") if ev else "",
        "evidence_url": ev.source_url if ev else "",
        "notes": "; ".join(notes),
        "candidate_id": c.candidate_id,
        **{k: "" for k in REVIEW_COLUMNS},
    }


def write_candidates_csv(spec: BriefSpec, cs: CandidateSet, path: Path) -> int:
    rows = candidate_rows(spec, cs)
    _write(path, CANDIDATE_COLUMNS, rows)
    return len(rows)


def write_organisations_csv(cs: CandidateSet, path: Path) -> int:
    rows = [
        {
            "group_id": o.group_id,
            "organisation": o.name,
            "include": "yes" if o.include else "no",
            "relationship": o.relationship,
            "evidence_quote": o.evidence[0].supporting_text if o.evidence else "",
            "evidence_url": o.evidence[0].source_url if o.evidence else "",
            "sources": str(len({e.source_url for e in o.evidence})),
        }
        for o in sorted(cs.organisations, key=lambda o: (o.group_id, -len(o.evidence), o.name.lower()))
    ]
    _write(path, ["group_id", "organisation", "include", "relationship", "evidence_quote", "evidence_url", "sources"],
           rows)
    return len(rows)


def write_review_files(spec: BriefSpec, cs: CandidateSet, output_dir: Path) -> list[Path]:
    written = [output_dir / CANDIDATES_CSV]
    write_candidates_csv(spec, cs, written[0])
    if cs.organisations:
        written.append(output_dir / ORGANISATIONS_CSV)
        write_organisations_csv(cs, written[1])
    return written


def _write(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _d(value) -> str:
    return value.isoformat()[:7] if value else ""




def main(argv: list[str] | None = None) -> int:
    from ..pipeline.artifacts import BRIEF_SPEC_FILE, CANDIDATES_FILE, load_brief_spec, load_candidates

    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m sourcing_agent.reporting.review_sheets <results folder>", file=sys.stderr)
        return 2
    folder = Path(args[0])
    spec = load_brief_spec(folder / BRIEF_SPEC_FILE)
    cs = load_candidates(folder / CANDIDATES_FILE)
    for path in write_review_files(spec, cs, folder):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

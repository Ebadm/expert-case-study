from __future__ import annotations

import csv
from datetime import date, datetime, timezone

import pytest

from sourcing_agent.brief.parser import build_spec
from sourcing_agent.models import (
    Candidate,
    CandidateSet,
    DiscoveredOrganisation,
    EnrichmentStats,
    Evidence,
    GeneratorInfo,
    ParserInfo,
    Position,
)
from sourcing_agent.reporting.review_sheets import CANDIDATE_COLUMNS, REVIEW_COLUMNS, write_review_files
from sourcing_agent.verification.employment import boundary_month, departure_status, relevant_organisations

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
T = "Tungsten Automation"


@pytest.fixture
def spec(tungsten_draft, tungsten_text):
    # former_employees window (run date 2026-10-10): left between 2023-10-10 and 2026-04-10.
    return build_spec(tungsten_draft, text=tungsten_text, brief_id="tungsten_automation", source_path="b.md",
                      run_date=date(2026, 10, 10), parser=ParserInfo(model="f", effort="medium", parsed_at=NOW))


def cand(name, *positions, group="former_employees") -> Candidate:
    return Candidate(candidate_id=f"{group}:{name}", group_id=group, name=name, profile_url=f"https://linkedin.com/in/{name}",
                     canonical_url=None, positions=list(positions), extraction_method="exa_entities",
                     evidence=[Evidence(source_url="u", source_type="profile", provider="exa", retrieved_at=NOW,
                                        claim=f"{name} claim", supporting_text="quote", evidence_type="provider_structured",
                                        supports=["employment"])])


def pos(org, start, end, current=False, matched=(T,)) -> Position:
    return Position(title="Engineer", organisation=org, start=start, end=end, is_current=current,
                    date_precision="month", matched_organisations=list(matched), source="exa_entities")


@pytest.mark.parametrize("positions, expected", [
    ([pos("Kofax", date(2020, 1, 1), date(2025, 1, 1))], "left_within_window"),
    ([pos("Kofax", date(2020, 1, 1), date(2026, 6, 1))], "left_too_recently"),     # inside the 6-month cooling-off
    ([pos("Kofax", date(2015, 1, 1), date(2021, 1, 1))], "left_too_long_ago"),
    # Month-level dates count in whole months: Apr 2026 is 6 months before Oct 2026, Oct 2023 is 36.
    ([pos("Kofax", date(2020, 1, 1), date(2026, 4, 1))], "left_within_window"),
    ([pos("Kofax", date(2018, 1, 1), date(2023, 10, 1))], "left_within_window"),
    ([pos("Kofax", date(2020, 1, 1), date(2026, 5, 1))], "left_too_recently"),
    ([pos("Kofax", date(2018, 1, 1), date(2023, 9, 1))], "left_too_long_ago"),
    ([pos(T, date(2016, 1, 1), None, current=True)], "still_current"),
    ([pos("Kofax", date(2015, 1, 1), date(2019, 1, 1)), pos(T, date(2024, 1, 1), None, current=True)], "still_current"),
    ([pos("Kofax", None, None)], "dates_unknown"),
    ([pos("Xerox", date(2020, 1, 1), None, current=True, matched=())], "no_relevant_position"),
    # The latest departure counts: rejoined and left again.
    ([pos("Kofax", date(2010, 1, 1), date(2015, 1, 1)), pos(T, date(2022, 1, 1), date(2024, 6, 1))], "left_within_window"),
])
def test_departure_status(spec, positions, expected):
    group = spec.group("former_employees")
    assert departure_status(cand("x", *positions), group, relevant_organisations(spec, group)) == expected


def test_review_csv_sorts_likely_eligible_first_and_has_reviewer_columns(spec, tmp_path):
    cs = CandidateSet(
        brief_id="t", source_sha256="s", extracted_at=NOW, extractor=GeneratorInfo(model="f", effort="m", generated_at=NOW),
        summary=[], enrichment=EnrichmentStats(),
        candidates=[
            cand("current", pos(T, date(2016, 1, 1), None, current=True)),
            cand("old", pos("Kofax", date(2010, 1, 1), date(2019, 1, 1))),
            cand("eligible", pos("Kofax", date(2020, 1, 1), date(2025, 1, 1)),
                 pos("Hyland", date(2025, 2, 1), None, current=True, matched=())),
            cand("skill_only", pos("Xerox", date(2020, 1, 1), None, current=True, matched=())),
        ],
        organisations=[DiscoveredOrganisation(group_id="customers", name="TUI Group", relationship="runs ReadSoft",
                                              evidence=[])],
    )
    paths = write_review_files(spec, cs, tmp_path)
    assert [p.name for p in paths] == ["candidates.csv", "organisations.csv"]
    raw = paths[0].read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    rows = list(csv.DictReader(paths[0].open(encoding="utf-8-sig")))
    assert list(rows[0].keys()) == CANDIDATE_COLUMNS
    assert [r["name"] for r in rows] == ["eligible", "old", "current", "skill_only"]
    first = rows[0]
    assert first["departure_status"] == "left_within_window"
    assert (first["relevant_organisation"], first["relevant_end"], first["left_relevant_org"]) == ("Kofax", "2025-01", "2025-01")
    assert first["current_employer"] == "Hyland"
    assert all(first[c] == "" for c in REVIEW_COLUMNS)


def test_day_precise_dates_are_compared_exactly(spec):
    group = spec.group("former_employees")
    p = pos("Kofax", date(2020, 1, 1), date(2026, 4, 20)).model_copy(update={"date_precision": "unknown"})
    assert departure_status(cand("x", p), group, relevant_organisations(spec, group)) == "left_too_recently"


def test_boundary_month_is_noted_in_the_review_sheet(spec, tmp_path):
    group = spec.group("former_employees")
    orgs = relevant_organisations(spec, group)
    edge = cand("edge", pos("Kofax", date(2020, 1, 1), date(2026, 4, 1)))
    assert boundary_month(edge, group, orgs) == date(2026, 4, 1)
    assert boundary_month(cand("mid", pos("Kofax", date(2020, 1, 1), date(2025, 1, 1))), group, orgs) is None

    cs = CandidateSet(brief_id="t", source_sha256="s", extracted_at=NOW, summary=[], enrichment=EnrichmentStats(),
                      extractor=GeneratorInfo(model="f", effort="m", generated_at=NOW), candidates=[edge], organisations=[])
    row = next(csv.DictReader(write_review_files(spec, cs, tmp_path)[0].open(encoding="utf-8-sig")))
    assert row["departure_status"] == "left_within_window"
    assert row["notes"].startswith("left in a boundary month (Apr 2026)")

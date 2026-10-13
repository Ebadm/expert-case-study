from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from sourcing_agent import pipeline
from sourcing_agent.brief.parser import (
    BriefParseError,
    build_spec,
    departure_window_dates,
    quote_in_text,
    subtract_months,
)
from sourcing_agent.config import LLMPricing, Settings
from sourcing_agent.models import BriefSpec, DepartureWindow, ParserInfo, Requirement

from .conftest import TUNGSTEN_BRIEF, FakeLLM

RUN_DATE = date(2026, 10, 10)
PARSER = ParserInfo(model="fake", effort="medium", parsed_at=datetime(2026, 10, 10, tzinfo=timezone.utc))


def _build(draft, text, run_date=RUN_DATE) -> BriefSpec:
    return build_spec(
        draft, text=text, brief_id="tungsten_automation", source_path="brief.md",
        run_date=run_date, parser=PARSER,
    )


# --- date arithmetic ------------------------------------------------------- #

@pytest.mark.parametrize(
    "d, months, expected",
    [
        (date(2026, 10, 10), 6, date(2026, 4, 10)),
        (date(2026, 10, 10), 36, date(2023, 10, 10)),
        (date(2026, 2, 15), 3, date(2025, 11, 15)),   # crosses a year boundary
        (date(2026, 8, 31), 6, date(2026, 2, 28)),    # clamps to month end
        (date(2028, 8, 31), 6, date(2028, 2, 29)),    # leap year
        (date(2026, 10, 10), 0, date(2026, 10, 10)),
    ],
)
def test_subtract_months(d, months, expected):
    assert subtract_months(d, months) == expected


def test_departure_window_is_computed_from_run_date():
    w = departure_window_dates(date(2026, 10, 10), min_months=6, max_months=36)
    assert w.left_on_or_after == date(2023, 10, 10)
    assert w.left_on_or_before == date(2026, 4, 10)

    later = departure_window_dates(date(2027, 1, 1), min_months=6, max_months=36)
    assert later.left_on_or_after == date(2024, 1, 1)
    assert later.left_on_or_before == date(2026, 7, 1)


def test_open_ended_window():
    w = departure_window_dates(RUN_DATE, min_months=None, max_months=24)
    assert w.left_on_or_after == date(2024, 10, 10)
    assert w.left_on_or_before is None


# --- spec building --------------------------------------------------------- #

def test_build_spec_enriches_groups(tungsten_draft, tungsten_text):
    spec = _build(tungsten_draft, tungsten_text)

    assert [g.group_id for g in spec.groups] == ["former_employees", "customers", "competitors"]
    former = spec.group("former_employees")
    assert former.departure_dates.left_on_or_after == date(2023, 10, 10)
    assert former.departure_dates.left_on_or_before == date(2026, 4, 10)
    assert spec.group("customers").departure_dates is None
    assert spec.group("competitors").departure_dates.left_on_or_after == date(2024, 10, 10)
    # "en" is always searched, even if the model left languages empty
    assert spec.group("competitors").query_languages == ["en"]
    assert spec.source.text == tungsten_text
    assert len(spec.source.sha256) == 64


def test_verbatim_quotes_produce_no_warnings(tungsten_draft, tungsten_text):
    spec = _build(tungsten_draft, tungsten_text)
    assert spec.validation_warnings == []


def test_invented_quote_is_flagged(tungsten_draft, tungsten_text):
    tungsten_draft.groups[1].hard_constraints.append(
        Requirement(description="Must have 10 years experience",
                    source_quote="Must have at least 10 years of experience.")
    )
    spec = _build(tungsten_draft, tungsten_text)
    assert len(spec.validation_warnings) == 1
    assert "customers hard constraint" in spec.validation_warnings[0]
    assert "10 years" in spec.validation_warnings[0]


def test_quote_matching_ignores_markdown_and_whitespace(tungsten_text):
    # Brief has "**Target:** Tungsten Automation —" with bold markers and an em dash.
    assert quote_in_text("Target: Tungsten Automation - intelligent document processing", tungsten_text)
    assert quote_in_text("No founders   or C-level", tungsten_text)
    assert not quote_in_text("Founders are welcome", tungsten_text)
    assert not quote_in_text("   ", tungsten_text)


def test_exclusion_group_ids_follow_normalisation(tungsten_draft, tungsten_text):
    tungsten_draft.excluded_organisations[0].applies_to_group_ids = ["Former Employees"]
    spec = _build(tungsten_draft, tungsten_text)
    assert spec.excluded_organisations[0].applies_to_group_ids == ["former_employees"]


def test_unknown_exclusion_group_widens_to_all_groups(tungsten_draft, tungsten_text):
    # Over-excluding is the safe failure mode for an exclusion rule.
    tungsten_draft.excluded_organisations[0].applies_to_group_ids = ["former_employees", "nope"]
    spec = _build(tungsten_draft, tungsten_text)
    assert spec.excluded_organisations[0].applies_to_group_ids == []
    assert any("unknown group 'nope'" in w for w in spec.validation_warnings)


def test_impossible_window_is_flagged(tungsten_draft, tungsten_text):
    tungsten_draft.groups[0].departure_window = DepartureWindow(
        min_months_since_departure=36, max_months_since_departure=6,
        source_quote="Left in the last 3 years",
    )
    spec = _build(tungsten_draft, tungsten_text)
    assert any("no candidate can satisfy it" in w for w in spec.validation_warnings)


def test_duplicate_group_ids_are_made_unique(tungsten_draft, tungsten_text):
    tungsten_draft.groups[2].group_id = "customers"
    spec = _build(tungsten_draft, tungsten_text)
    assert [g.group_id for g in spec.groups] == ["former_employees", "customers", "customers_2"]


def test_no_groups_is_an_error(tungsten_draft, tungsten_text):
    tungsten_draft.groups = []
    with pytest.raises(BriefParseError):
        _build(tungsten_draft, tungsten_text)


# --- pipeline / artifacts -------------------------------------------------- #

def test_pipeline_writes_round_trippable_artifacts(tmp_path, tungsten_draft):
    llm = FakeLLM(tungsten_draft)
    settings = Settings(
        llm_model="fake", llm_effort="medium", anthropic_api_key=None,
        llm_pricing=LLMPricing(0.10, 0.50, source="test"),
    )
    result = pipeline.run(
        tmp_path, settings=settings, brief_path=TUNGSTEN_BRIEF, run_date=RUN_DATE,
        stop_after="brief_parsing", llm=llm,
    )

    assert len(llm.calls) == 1
    assert "Kofax" in llm.calls[0]["user"]  # the brief text was sent
    assert result.queries is None
    assert not (tmp_path / pipeline.QUERIES_FILE).exists()

    spec_path = tmp_path / pipeline.BRIEF_SPEC_FILE
    loaded = pipeline.load_brief_spec(spec_path)
    assert loaded == result.spec
    assert loaded.brief_id == "tungsten_automation"
    assert loaded.run_date == RUN_DATE

    meta = json.loads((tmp_path / pipeline.RUN_METADATA_FILE).read_text(encoding="utf-8"))
    assert meta["stopped_after"] == "brief_parsing"
    assert [s["stage"] for s in meta["stages"]] == ["brief_parsing"]
    assert meta["errors"] == []


def test_pipeline_records_failures_in_metadata(tmp_path, tungsten_draft):
    tungsten_draft.groups = []
    settings = Settings(
        llm_model="fake", llm_effort="medium", anthropic_api_key=None,
        llm_pricing=LLMPricing(None, None, source="unknown"),
    )
    with pytest.raises(BriefParseError):
        pipeline.run(
            tmp_path, settings=settings, brief_path=TUNGSTEN_BRIEF, run_date=RUN_DATE,
            llm=FakeLLM(tungsten_draft),
        )

    assert not (tmp_path / pipeline.BRIEF_SPEC_FILE).exists()
    meta = json.loads((tmp_path / pipeline.RUN_METADATA_FILE).read_text(encoding="utf-8"))
    assert meta["errors"] and "no expert groups" in meta["errors"][0]

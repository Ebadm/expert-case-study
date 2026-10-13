from __future__ import annotations

from pathlib import Path

import pytest

from sourcing_agent.models import (
    BriefDraft,
    DepartureWindow,
    ExpertGroupDraft,
    Organisation,
    OrganisationExclusion,
    Requirement,
)

ROOT = Path(__file__).resolve().parents[1]
TUNGSTEN_BRIEF = ROOT / "briefs" / "raw" / "tungsten-automation.md"


class FakeLLM:
    """Returns canned outputs keyed by output model; records what it was asked.

    A response may be a model instance or a callable ``(user_prompt) -> instance``.
    Asking for an output model with no response configured fails the test.
    """

    def __init__(self, *responses, by_model: dict | None = None) -> None:
        self.by_model = {type(r): r for r in responses} | (by_model or {})
        self.calls: list[dict] = []

    def parse(self, *, stage, system, user, output_model):
        self.calls.append({"stage": stage, "system": system, "user": user, "output_model": output_model})
        response = self.by_model[output_model]
        return response(user) if callable(response) else response


def _group(**overrides) -> ExpertGroupDraft:
    base = dict(
        group_id="group",
        name="Group",
        description="",
        eligible_organisations=[],
        eligible_organisation_types=[],
        required_employment_status="current_or_former",
        departure_window=None,
        target_roles=[],
        excluded_roles=[],
        relevant_topics=[],
        required_evidence=[],
        hard_constraints=[],
        preferences=[],
        screening_questions=[],
        search_strategy="",
        query_languages=["en"],
    )
    base.update(overrides)
    return ExpertGroupDraft(**base)


@pytest.fixture
def tungsten_text() -> str:
    return TUNGSTEN_BRIEF.read_text(encoding="utf-8")


@pytest.fixture
def tungsten_draft() -> BriefDraft:
    """A hand-written draft of what a correct parse of the Tungsten brief looks like."""
    tungsten = Organisation(name="Tungsten Automation", aliases=["Kofax"])
    return BriefDraft(
        title="Tungsten Automation commercial and technical due diligence",
        client="fictional global private-equity firm",
        project_type="commercial and technical due diligence",
        objective="Evaluate product competitiveness, retention and outlook after the rebrand.",
        target_organisation=tungsten,
        sector="Intelligent document processing",
        excluded_organisations=[
            OrganisationExclusion(
                organisation=tungsten,
                scope="current_employees",
                applies_to_group_ids=[],
                source_quote="Do not include current employees of Tungsten Automation.",
            )
        ],
        global_hard_constraints=[],
        global_preferences=[],
        groups=[
            _group(
                group_id="Former Employees",
                name="Former employees",
                eligible_organisations=[tungsten],
                required_employment_status="former",
                departure_window=DepartureWindow(
                    min_months_since_departure=6,
                    max_months_since_departure=36,
                    source_quote="Left in the last 3 years ... Former employees must have left at least 6 months ago.",
                ),
                hard_constraints=[
                    Requirement(description="Left within 3 years", source_quote="Left in the last 3 years."),
                ],
            ),
            _group(group_id="customers", name="Customers"),
            _group(
                group_id="competitors",
                name="Competitors",
                eligible_organisations=[Organisation(name=n, aliases=[]) for n in
                                        ["ABBYY", "Hyland", "OpenText", "IBM", "Microsoft"]],
                departure_window=DepartureWindow(
                    min_months_since_departure=None,
                    max_months_since_departure=24,
                    source_quote="Current or former (up to 2 years out)",
                ),
                excluded_roles=["founder", "C-level"],
                hard_constraints=[Requirement(description="No founders or C-level", source_quote="No founders or C-level.")],
                query_languages=[],
            ),
        ],
        ambiguities=[],
    )

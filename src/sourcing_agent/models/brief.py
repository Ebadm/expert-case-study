"""The brief: the LLM's draft schema and the validated spec (stage 1).

* ``BriefDraft`` / ``ExpertGroupDraft`` - the exact schema the LLM fills in.
  Everything is relative (months, not dates) and every rule carries a verbatim
  ``source_quote`` so it can be checked against the brief.
* ``BriefSpec`` / ``ExpertGroup`` - the draft plus fields only code is allowed
  to produce: the run date, absolute date windows, provenance and validation
  warnings. This is what later stages consume.

The draft models avoid ``dict`` fields and defaults on purpose: structured
outputs need closed object schemas with every property required.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from .common import SCHEMA_VERSION, EmploymentStatus, ExclusionScope


class Organisation(BaseModel):
    name: str = Field(description="Canonical (current) organisation name.")
    aliases: list[str] = Field(
        description=(
            "Former names, trading names or abbreviations that the brief itself "
            "states refer to this organisation. Do not add aliases from your own "
            "knowledge."
        )
    )


class Requirement(BaseModel):
    description: str = Field(description="The rule, restated precisely in one sentence.")
    source_quote: str = Field(
        description="Verbatim span copied from the brief that this rule comes from."
    )


class DepartureWindow(BaseModel):
    """How long ago a former employee must have left, relative to the run date."""

    min_months_since_departure: int | None = Field(
        description="Must have left at least this many months ago (e.g. 6). null if no minimum."
    )
    max_months_since_departure: int | None = Field(
        description="Must have left at most this many months ago (e.g. 36 for 3 years). null if no maximum."
    )
    source_quote: str = Field(description="Verbatim span(s) from the brief stating the window.")


class OrganisationExclusion(BaseModel):
    organisation: Organisation
    scope: ExclusionScope = Field(
        description=(
            "'current_employees' when the brief only excludes people currently "
            "working there (former employees may still qualify)."
        )
    )
    applies_to_group_ids: list[str] = Field(
        description="group_ids this exclusion applies to. Empty list means all groups."
    )
    source_quote: str


class ExpertGroupDraft(BaseModel):
    group_id: str = Field(description="Short snake_case id, e.g. 'former_employees'.")
    name: str = Field(description="Human-readable group name.")
    description: str = Field(description="Who this group is and what they should be able to discuss.")
    eligible_organisations: list[Organisation] = Field(
        description="Named organisations whose (current or former) staff form this group. Empty if none are named."
    )
    eligible_organisation_types: list[str] = Field(
        description=(
            "Kinds of organisation that qualify when none are named, e.g. "
            "'enterprise customers of the target', 'private rail freight operators active in Poland'."
        )
    )
    required_employment_status: EmploymentStatus = Field(
        description="Required relationship to the eligible organisations."
    )
    departure_window: DepartureWindow | None = Field(
        description="Only if the brief states a time window for former employees of this group; otherwise null."
    )
    target_roles: list[str] = Field(description="Job titles / functions the brief names as ideal.")
    excluded_roles: list[str] = Field(
        description="Roles or seniority levels the brief explicitly rules out, e.g. 'founder', 'C-level'."
    )
    relevant_topics: list[str] = Field(
        description="Products, technologies or subject areas the expert should know."
    )
    required_evidence: list[str] = Field(
        description=(
            "What public evidence would prove someone belongs in this group, "
            "e.g. 'named customer case study showing product usage'."
        )
    )
    hard_constraints: list[Requirement] = Field(
        description="Rules a candidate MUST satisfy. Only rules the brief states as mandatory."
    )
    preferences: list[Requirement] = Field(
        description="Desirable attributes that should affect ranking but not eligibility ('ideally', 'expected', 'preferred')."
    )
    screening_questions: list[str] = Field(description="Screening questions for this group, verbatim if given.")
    search_strategy: str = Field(
        description="Where people in this group are most likely to be found publicly and what to search for."
    )
    query_languages: list[str] = Field(
        description="ISO 639-1 codes of languages to search in, e.g. ['en'] or ['en', 'pl']."
    )


class BriefDraft(BaseModel):
    title: str = Field(description="Short title for the project.")
    client: str | None
    project_type: str | None
    objective: str = Field(description="One-paragraph summary of what the client wants to learn.")
    target_organisation: Organisation | None = Field(
        description="The company under study, if the brief is about one company; null for sector studies."
    )
    sector: str = Field(description="Industry / market the brief covers.")
    excluded_organisations: list[OrganisationExclusion]
    global_hard_constraints: list[Requirement] = Field(
        description="Mandatory rules that apply to every group."
    )
    global_preferences: list[Requirement] = Field(
        description="Preferences that apply to every group (e.g. language, years of experience stated as 'ideally')."
    )
    groups: list[ExpertGroupDraft]
    ambiguities: list[str] = Field(
        description=(
            "Points where the brief is unclear or could be read more than one way, "
            "and how you interpreted them. Empty list if none."
        )
    )


class DateWindow(BaseModel):
    """Absolute departure-date bounds, computed from run_date in code."""

    left_on_or_after: date | None
    left_on_or_before: date | None


class ExpertGroup(ExpertGroupDraft):
    departure_dates: DateWindow | None = None


class SourceInfo(BaseModel):
    path: str
    sha256: str
    text: str


class ParserInfo(BaseModel):
    model: str
    effort: str
    parsed_at: datetime


class BriefSpec(BriefDraft):
    schema_version: str = SCHEMA_VERSION
    brief_id: str
    run_date: date
    groups: list[ExpertGroup]
    source: SourceInfo
    parser: ParserInfo
    validation_warnings: list[str] = Field(default_factory=list)

    def group(self, group_id: str) -> ExpertGroup:
        for g in self.groups:
            if g.group_id == group_id:
                return g
        raise KeyError(group_id)

"""Entity resolution, verification and ranking records (stages 7-9)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .candidates import Candidate, DiscoveredOrganisation
from .common import SCHEMA_VERSION, Decision, RuleOutcome


class MergeLink(BaseModel):
    """Why two records were judged to be the same person."""

    a: str  # candidate_id
    b: str
    rule: Literal["same_profile_url", "name_and_organisation", "profile_lookup", "llm_same_person"]
    detail: str


class Identity(BaseModel):
    """One real person, however many records, queries, pages or groups found them."""

    identity_id: str
    name: str
    profile_urls: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)  # the source records merged into it
    links: list[MergeLink] = Field(default_factory=list)


class SamePersonDraft(BaseModel):
    """LLM answer for an ambiguous pair of records (similar names, same employer)."""

    same_person: bool = Field(description="True only when the records clearly describe one person.")
    reason: str = Field(description="One sentence naming the facts that decided it.")


class ResolutionStats(BaseModel):
    records_in: int
    identities: int
    records_out: int  # one record per identity per group
    merged_records: int  # records_in - records_out
    within_group_duplicates: int  # records folded into another record of the same group
    cross_group_identities: int  # identities listed in more than one group
    ambiguous_pairs: int  # sent to the LLM
    llm_merges: int


class ResolvedSet(BaseModel):
    """Stage 6 output: canonical identities and one merged candidate per identity per group."""

    schema_version: str = SCHEMA_VERSION
    brief_id: str
    source_sha256: str
    resolved_at: datetime
    stats: ResolutionStats
    identities: list[Identity]
    candidates: list[Candidate]
    organisations: list[DiscoveredOrganisation] = Field(default_factory=list)

    def for_group(self, group_id: str) -> list[Candidate]:
        return [c for c in self.candidates if c.group_id == group_id]


class RuleResult(BaseModel):
    rule: str  # e.g. "employment_status", "exclusion", "excluded_role"
    outcome: RuleOutcome
    detail: str


class Verification(BaseModel):
    candidate_id: str
    identity_id: str | None
    group_id: str
    name: str
    decision: Decision
    departure_status: str
    rules: list[RuleResult]

    def reasons(self) -> list[RuleResult]:
        """The rules that made the decision (failures, else reviews)."""
        fails = [r for r in self.rules if r.outcome == "fail"]
        return fails or [r for r in self.rules if r.outcome == "review"]


class VerificationSet(BaseModel):
    schema_version: str = SCHEMA_VERSION
    brief_id: str
    source_sha256: str
    verified_at: datetime
    decisions: list[Verification]

    def get(self, group_id: str, candidate_id: str) -> Verification | None:
        return next((v for v in self.decisions if v.group_id == group_id and v.candidate_id == candidate_id), None)


class CandidateAssessmentDraft(BaseModel):
    """The LLM's judgement of one eligible candidate against the group (ranking)."""

    ref: str = Field(description='The candidate reference given in the input, e.g. "C3".')
    verdict: Literal["meets", "unclear", "does_not_meet"] = Field(
        description="meets: the dossier shows every hard requirement. unclear: nothing contradicts them, but "
                    "the dossier does not show one (e.g. a department head whose department is not named). "
                    "does_not_meet: a hard requirement is contradicted (an unrelated function, the wrong "
                    "organisation or relationship, a non-lead role where a lead is required)."
    )
    unmet_requirement: str | None = Field(description="The requirement not shown or not met; null when meets.")
    role_fit: int = Field(description="0-5: how well the relevant role matches the group's target roles.")
    relevance: int = Field(
        description="0-5: how close the person's work is to the brief's topics and the group's preferences."
    )
    seniority: int = Field(description="0-5: seniority for the group (individual contributor 1-2, lead 3, "
                                       "head or director 4, VP 5).")
    evidence_strength: int = Field(
        description="0-5: how directly the evidence shows the relevant role and the group's required evidence "
                    "(a dated employment record plus a supporting quote is high; a bare title is low)."
    )
    reason: str = Field(description="One sentence on why this person is relevant, using only facts in the dossier.")
    evidence_ref: str = Field(description='The single evidence item that best supports the reason, e.g. "C3.E2".')


class RankingDraft(BaseModel):
    assessments: list[CandidateAssessmentDraft]


class ScoreBreakdown(BaseModel):
    """Component scores, each 0-1, and the weighted total (0-100)."""

    role_fit: float
    relevance: float
    seniority: float
    evidence_strength: float
    recency: float  # code: current, or how recently they left within the window
    corroboration: float  # code: independent sources (distinct sites, structured record + page)
    profile_penalty: float = 0.0  # points taken off because the expert has no profile URL
    total: float


class ExpertEvidence(BaseModel):
    claim: str
    quote: str | None
    url: str
    # "employment_record": a structured profile record (claim built by code from it);
    # "quote": verbatim text from a profile or page.
    kind: Literal["employment_record", "quote", "snippet"] = "quote"

    def display(self) -> str:
        if self.kind == "employment_record":
            return f"Employment record: {self.claim}"
        return f'"{self.quote or self.claim}"'


class Expert(BaseModel):
    """One shortlisted expert, as returned to the researcher."""

    rank: int
    name: str
    title: str | None
    company: str
    profile_url: str | None
    reason: str
    evidence: ExpertEvidence
    status: str  # e.g. "left Apr 2026", "current"
    score: ScoreBreakdown
    sources: list[str]
    location: str | None = None
    also_in_groups: list[str] = Field(default_factory=list)
    identity_id: str | None = None
    candidate_id: str


class Assessment(BaseModel):
    """Ranking record for every candidate the LLM assessed, returned or not."""

    group_id: str
    candidate_id: str
    name: str
    verdict: Literal["meets", "unclear", "does_not_meet"]
    unmet_requirement: str | None
    reason: str
    evidence_ref: str
    score: ScoreBreakdown
    # Hash of everything the verdict depends on (prompt, group definition, dossier, model):
    # a later ranking of an unchanged dossier reuses this assessment instead of asking again.
    key: str | None = None


class GroupResult(BaseModel):
    group_id: str
    name: str
    candidates: int  # resolved records in the group
    eligible: int  # passed deterministic verification
    needs_review: int
    rejected_by_ranking: int  # eligible, but the LLM found a hard requirement contradicted
    unclear: int  # eligible, but the dossier does not show a hard requirement
    experts: list[Expert]
    # Best "unclear" candidates, listed separately for a researcher to check; never counted as experts.
    for_review: list[Expert] = Field(default_factory=list)


class FinalResults(BaseModel):
    schema_version: str = SCHEMA_VERSION
    brief_id: str
    title: str
    source_sha256: str
    run_date: date
    generated_at: datetime
    max_per_group: int
    weights: dict[str, float]
    no_profile_penalty: float = 0.0
    groups: list[GroupResult]
    assessments: list[Assessment] = Field(default_factory=list)

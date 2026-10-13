"""Candidates, their evidence and discovered organisations (stages 4-6)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .common import SCHEMA_VERSION, EvidenceType, ExtractionMethod, Provider
from .queries import GeneratorInfo


class Evidence(BaseModel):
    source_url: str
    source_type: Literal["profile", "web_page", "search_snippet"]
    provider: Provider | None
    retrieved_at: datetime
    claim: str
    supporting_text: str | None
    evidence_type: EvidenceType
    supports: list[str]  # e.g. ["identity", "employment", "dates", "role", "customer_relationship"]


class Position(BaseModel):
    title: str | None
    organisation: str
    start: date | None = None
    end: date | None = None
    # True only when a structured source shows an open-ended role; None when unknown.
    is_current: bool | None = None
    date_precision: Literal["month", "unknown"] = "unknown"
    # Brief organisations this employer was matched to (canonical names), by code.
    matched_organisations: list[str] = Field(default_factory=list)
    source: ExtractionMethod


class Candidate(BaseModel):
    candidate_id: str
    group_id: str
    name: str
    profile_url: str | None
    canonical_url: str | None
    headline: str | None = None
    location: str | None = None
    positions: list[Position] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    extraction_method: ExtractionMethod
    # When the provider last saw the source (Exa publishedDate), so stale data is visible.
    data_as_of: str | None = None
    source_hit_ids: list[str] = Field(default_factory=list)
    providers: list[Provider] = Field(default_factory=list)
    query_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # Whether the profile or page text names the target (or an alias): for customer
    # groups, the evidence of hands-on experience with its products. None = no text.
    target_mentioned: bool | None = None
    verification_status: Literal["unverified"] = "unverified"
    # Set by entity resolution: the canonical person this record belongs to, and the
    # other groups the same person was found in (they stay listed in each group).
    identity_id: str | None = None
    also_in_groups: list[str] = Field(default_factory=list)
    # People named on a web page: outcome of the search for their own profile (profile
    # lookup), and the profile record found for them, which entity resolution merges in.
    profile_lookup: Literal["found", "not_found", "ambiguous"] | None = None
    profile_lookup_version: int | None = None  # LOOKUP_VERSION that produced profile_lookup
    same_person_as: list[str] = Field(default_factory=list)


class DiscoveredOrganisation(BaseModel):
    """An organisation a web page says qualifies for a company-discovery group."""

    group_id: str
    name: str
    relationship: str
    evidence: list[Evidence]
    # Reviewers can set this to false in organisations.json to skip the people search there.
    include: bool = True

    @property
    def sources(self) -> int:
        return len({e.source_url for e in self.evidence})


class OrganisationSet(BaseModel):
    """Organisations found by company discovery: the hand-off to the people search at them.

    Written as organisations.json; can be reviewed or edited before that search runs.
    """

    schema_version: str = SCHEMA_VERSION
    brief_id: str
    source_sha256: str
    created_at: datetime
    organisations: list[DiscoveredOrganisation]

    def for_group(self, group_id: str, *, included_only: bool = True) -> list[DiscoveredOrganisation]:
        orgs = [o for o in self.organisations if o.group_id == group_id and (o.include or not included_only)]
        return sorted(orgs, key=lambda o: (-o.sources, o.name.lower()))


class ProfilePositionDraft(BaseModel):
    title: str | None = Field(description="Job title exactly as written, or null.")
    organisation: str = Field(description="Employer exactly as written.")
    start: str | None = Field(description="Start as YYYY-MM if the text gives it, else null.")
    end: str | None = Field(description="End as YYYY-MM, 'present' for a current role, or null if not given.")
    quote: str = Field(description="The verbatim line(s) from the profile that state this position and its dates.")


class ProfileDraft(BaseModel):
    name: str = Field(description="The person's name exactly as written.")
    headline: str | None
    positions: list[ProfilePositionDraft]


class PagePersonDraft(BaseModel):
    name: str = Field(description="Full name exactly as written on the page.")
    title: str | None = Field(description="Their job title as written, or null.")
    organisation: str | None = Field(description="The organisation they represent on this page, as written.")
    relevance: str = Field(description="One sentence: why this person may fit the expert group, based only on the page.")
    quote: str = Field(description="Verbatim text from the page that names the person and shows their role or involvement.")


class PageOrganisationDraft(BaseModel):
    name: str = Field(description="Organisation name as written on the page.")
    relationship: str = Field(description="How the page shows it qualifies, e.g. 'runs Kofax capture for invoice processing'.")
    quote: str = Field(description="Verbatim text from the page that shows this.")


class PageExtractionDraft(BaseModel):
    people: list[PagePersonDraft] = Field(description="Named people on the page who could belong to the expert group.")
    organisations: list[PageOrganisationDraft] = Field(
        description="Organisations the page shows qualify for the expert group's organisation types."
    )


class CandidateSummaryRow(BaseModel):
    group_id: str
    candidates: int
    by_method: dict[str, int]
    with_matched_position: int  # has a position at an eligible / target organisation
    useful: int = 0  # dates fit the group's employment rule (the pool ranking picks from)
    organisations: int  # discovered organisations (company-discovery groups)


class EnrichmentStats(BaseModel):
    requested: int = 0
    succeeded: int = 0
    failed: int = 0
    cost_usd: float | None = 0.0


class CandidateSet(BaseModel):
    schema_version: str = SCHEMA_VERSION
    brief_id: str
    source_sha256: str
    extracted_at: datetime
    extractor: GeneratorInfo
    summary: list[CandidateSummaryRow]
    enrichment: EnrichmentStats
    candidates: list[Candidate]
    organisations: list[DiscoveredOrganisation]
    warnings: list[str] = Field(default_factory=list)

    def for_group(self, group_id: str) -> list[Candidate]:
        return [c for c in self.candidates if c.group_id == group_id]


class ProfileLookup(BaseModel):
    """The search for the profile of one person named on a web page."""

    group_id: str
    candidate_id: str  # the page record
    name: str
    organisation: str
    query: str
    outcome: Literal["found", "not_found", "ambiguous", "error"]
    profile_url: str | None = None  # accepted profile (outcome "found")
    profile_candidate_id: str | None = None
    # Profiles that passed: the same name, and the organisation in the result or work history.
    matches: list[str] = Field(default_factory=list)
    # How the profile was accepted: the result named the organisation, or its work history did.
    accepted_on: Literal["search_result", "work_history", "profile_text"] | None = None


class ProfileLookupSet(BaseModel):
    schema_version: str = SCHEMA_VERSION
    brief_id: str
    source_sha256: str
    looked_up_at: datetime
    lookups: list[ProfileLookup]

    def counts(self) -> dict[str, int]:
        out = {"looked_up": len(self.lookups)}
        for x in self.lookups:
            out[x.outcome] = out.get(x.outcome, 0) + 1
        return out

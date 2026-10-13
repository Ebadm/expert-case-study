"""Where a run starts: the stage order, and the saved artifact a run resumes from.

A run starts from exactly one input: the raw brief, or a saved brief_spec.json,
queries.json, search_results.json, organisations.json or candidates.json. A saved artifact
needs the brief_spec.json in its folder (organisations.json also its candidates.json), and
every artifact must come from the same brief (same SHA-256) as that spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..brief.parser import STAGE as BRIEF_STAGE
from ..brief.parser import brief_id_from_path
from ..extraction.candidate_extractor import STAGE as EXTRACTION_STAGE
from ..models import BriefSpec, CandidateSet, OrganisationSet, QueryPlan, SearchResults
from ..people_search.organisations import STAGE as ORG_PEOPLE_STAGE
from ..people_search.profile_lookup import STAGE as PROFILE_LOOKUP_STAGE
from ..ranking.assessor import STAGE as RANKING_STAGE
from ..resolution.identity import STAGE as RESOLUTION_STAGE
from ..search.query_generator import STAGE as QUERY_STAGE
from ..search.runner import STAGE as SEARCH_STAGE
from ..verification.eligibility import STAGE as VERIFICATION_STAGE
from .artifacts import (
    BRIEF_SPEC_FILE,
    CANDIDATES_FILE,
    QUERIES_FILE,
    load_brief_spec,
    load_candidates,
    load_organisations,
    load_query_plan,
    load_search_results,
)

STAGES = (
    BRIEF_STAGE,
    QUERY_STAGE,
    SEARCH_STAGE,
    EXTRACTION_STAGE,
    ORG_PEOPLE_STAGE,
    PROFILE_LOOKUP_STAGE,
    RESOLUTION_STAGE,
    VERIFICATION_STAGE,
    RANKING_STAGE,
)
# Stages that call the LLM (verification is code only, search calls no model).
LLM_STAGES = (BRIEF_STAGE, QUERY_STAGE, EXTRACTION_STAGE, ORG_PEOPLE_STAGE, PROFILE_LOOKUP_STAGE,
              RESOLUTION_STAGE, RANKING_STAGE)


@dataclass
class Start:
    """What a run starts with: its first stage and the artifacts loaded for it."""

    first: str
    brief_id: str
    run_date: date
    spec_path: Path | None = None  # None when starting from the raw brief
    source: Path | None = None  # the saved artifact resumed from
    spec: BriefSpec | None = None
    queries: QueryPlan | None = None
    search: SearchResults | None = None
    candidates: CandidateSet | None = None
    organisations: OrganisationSet | None = None

    def runs(self, stage: str, stop_after: str) -> bool:
        return STAGES.index(self.first) <= STAGES.index(stage) <= STAGES.index(stop_after)

    def needs_llm(self, stop_after: str) -> bool:
        return any(self.runs(s, stop_after) for s in LLM_STAGES)


def step(stage: str) -> str:
    """The stage's position, e.g. "4/9"."""
    return f"{STAGES.index(stage) + 1}/{len(STAGES)}"


def load_start(
    *,
    brief_path: Path | None,
    spec_path: Path | None,
    queries_path: Path | None,
    search_path: Path | None,
    organisations_path: Path | None,
    candidates_path: Path | None,
    run_date: date | None,
    stop_after: str,
) -> Start:
    """Check the inputs, load the saved artifacts and their companions, verify they match.

    With a saved spec (or anything later), the spec's own run_date is used, because its
    date windows were computed from it.
    """
    given = [p for p in (brief_path, spec_path, queries_path, search_path, organisations_path, candidates_path)
             if p is not None]
    if len(given) != 1:
        raise ValueError("pass exactly one of brief_path, spec_path, queries_path, search_path, "
                         "organisations_path or candidates_path")
    if stop_after not in STAGES:
        raise ValueError(f"stop_after must be one of {STAGES}")
    first = (BRIEF_STAGE if brief_path else QUERY_STAGE if spec_path else SEARCH_STAGE if queries_path
             else EXTRACTION_STAGE if search_path else ORG_PEOPLE_STAGE if organisations_path
             else PROFILE_LOOKUP_STAGE)
    if STAGES.index(stop_after) < STAGES.index(first):
        raise ValueError(f"cannot stop after {stop_after!r} when starting at {first!r}")

    if brief_path:
        return Start(first=first, brief_id=brief_id_from_path(brief_path), run_date=run_date or date.today())

    source = given[0]
    folder = source.parent
    spec_path = spec_path or folder / BRIEF_SPEC_FILE
    needed = [spec_path] + ([folder / CANDIDATES_FILE] if organisations_path else [])
    for path in needed:
        if not path.exists():
            raise FileNotFoundError(f"{path} (needed next to {source.name})")

    start = Start(first=first, brief_id="", run_date=date.today(), spec_path=spec_path,
                  source=None if source == spec_path else source)
    if candidates_path:
        start.candidates = load_candidates(candidates_path)
    if organisations_path:
        start.organisations = load_organisations(organisations_path)
        start.candidates = load_candidates(folder / CANDIDATES_FILE)
    if search_path:
        start.search = load_search_results(search_path)
    if queries_path:
        start.queries = load_query_plan(queries_path)
    elif (search_path or organisations_path) and (folder / QUERIES_FILE).exists():
        start.queries = load_query_plan(folder / QUERIES_FILE)  # role, topic and title words for stage 5

    spec = start.spec = load_brief_spec(spec_path)
    for artifact, path in ((start.queries, queries_path or folder / QUERIES_FILE), (start.search, search_path),
                           (start.organisations, organisations_path),
                           (start.candidates, candidates_path or folder / CANDIDATES_FILE)):
        if artifact is not None and artifact.source_sha256 != spec.source.sha256:
            raise ValueError(f"{path} was generated from a different brief than {spec_path}")
    start.brief_id, start.run_date = spec.brief_id, spec.run_date
    return start

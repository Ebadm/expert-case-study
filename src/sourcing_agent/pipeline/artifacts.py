"""Artifact files: names, loading and saving.

Each stage reads the previous stage's artifact from the run folder, so a run can start
at any stage from a saved (or hand-reviewed) file. Every artifact records the brief's
SHA-256; the orchestrator refuses one generated from a different brief.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import (
    BriefSpec,
    CandidateSet,
    FinalResults,
    OrganisationSet,
    ProfileLookupSet,
    QueryPlan,
    RunMetadata,
    SearchResults,
)

log = logging.getLogger(__name__)

BRIEF_SPEC_FILE = "brief_spec.json"
QUERIES_FILE = "queries.json"
SEARCH_RESULTS_FILE = "search_results.json"
CANDIDATES_FILE = "candidates.json"
ORGANISATIONS_FILE = "organisations.json"
ORG_PEOPLE_QUERIES_FILE = "org_people_queries.json"
ORG_PEOPLE_SEARCH_FILE = "org_people_search_results.json"
PROFILE_LOOKUP_FILE = "profile_lookup.json"
RESOLVED_FILE = "resolved_candidates.json"
VERIFICATION_FILE = "verification.json"
FINAL_RESULTS_FILE = "final_results.json"
RUN_METADATA_FILE = "run_metadata.json"
RUN_HISTORY_FILE = "run_history.jsonl"


def previous_final(output_dir: Path) -> FinalResults | None:
    """An earlier run's final_results.json in this folder, for reusing its verdicts."""
    path = output_dir / FINAL_RESULTS_FILE
    if not path.exists():
        return None
    try:
        return FinalResults.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:  # an older schema: judge afresh
        log.warning("could not reuse verdicts from %s", path)
        return None


def load_brief_spec(path: Path) -> BriefSpec:
    """Load a saved spec (for later stages, or after a human has reviewed/edited it)."""
    return BriefSpec.model_validate_json(path.read_text(encoding="utf-8"))


def load_query_plan(path: Path) -> QueryPlan:
    return QueryPlan.model_validate_json(path.read_text(encoding="utf-8"))


def load_search_results(path: Path) -> SearchResults:
    return SearchResults.model_validate_json(path.read_text(encoding="utf-8"))


def load_candidates(path: Path) -> CandidateSet:
    return CandidateSet.model_validate_json(path.read_text(encoding="utf-8"))


def load_organisations(path: Path) -> OrganisationSet:
    return OrganisationSet.model_validate_json(path.read_text(encoding="utf-8"))


def load_profile_lookups(path: Path) -> ProfileLookupSet | None:
    """Lookups saved by earlier runs in this folder (None when there are none)."""
    if not path.exists():
        return None
    return ProfileLookupSet.model_validate_json(path.read_text(encoding="utf-8"))


def copy_if_elsewhere(dest: Path, src: Path | None, payload: str) -> None:
    """Keep each output directory self-contained when the input came from another folder."""
    if src is not None and dest.resolve() != src.resolve():
        write_json(dest, payload)


def write_metadata(output_dir: Path, metadata: RunMetadata) -> None:
    """Latest run in run_metadata.json; every run appended to run_history.jsonl.

    Several runs share one folder (e.g. parse, then regenerate queries from the
    saved spec), so the history keeps each run's cost and timing.
    """
    write_json(output_dir / RUN_METADATA_FILE, metadata.model_dump_json(indent=2))
    with (output_dir / RUN_HISTORY_FILE).open("a", encoding="utf-8") as f:
        f.write(metadata.model_dump_json() + "\n")


def write_json(path: Path, payload: str) -> None:
    path.write_text(payload + "\n", encoding="utf-8")
    log.info("wrote %s", path)

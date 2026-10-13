"""The pipeline: stage orchestration (``orchestrator``) and artifact files (``artifacts``).

    from sourcing_agent import pipeline
    result = pipeline.run(output_dir, settings=settings, brief_path=path)
"""

from ..brief.parser import brief_id_from_path
from .artifacts import (
    BRIEF_SPEC_FILE,
    CANDIDATES_FILE,
    FINAL_RESULTS_FILE,
    ORG_PEOPLE_QUERIES_FILE,
    ORG_PEOPLE_SEARCH_FILE,
    ORGANISATIONS_FILE,
    PROFILE_LOOKUP_FILE,
    QUERIES_FILE,
    RESOLVED_FILE,
    RUN_HISTORY_FILE,
    RUN_METADATA_FILE,
    SEARCH_RESULTS_FILE,
    VERIFICATION_FILE,
    load_brief_spec,
    load_candidates,
    load_organisations,
    load_query_plan,
    load_search_results,
)
from .orchestrator import PipelineResult, run
from .start import STAGES

__all__ = [
    "BRIEF_SPEC_FILE",
    "CANDIDATES_FILE",
    "FINAL_RESULTS_FILE",
    "ORG_PEOPLE_QUERIES_FILE",
    "ORG_PEOPLE_SEARCH_FILE",
    "ORGANISATIONS_FILE",
    "PROFILE_LOOKUP_FILE",
    "QUERIES_FILE",
    "RESOLVED_FILE",
    "RUN_HISTORY_FILE",
    "RUN_METADATA_FILE",
    "SEARCH_RESULTS_FILE",
    "STAGES",
    "VERIFICATION_FILE",
    "PipelineResult",
    "brief_id_from_path",
    "load_brief_spec",
    "load_candidates",
    "load_organisations",
    "load_query_plan",
    "load_search_results",
    "run",
]

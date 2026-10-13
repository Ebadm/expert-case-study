"""Stage orchestration: ``run`` executes the stages in order; each reads the previous stage's artifact.

Stages:
    1. brief_parsing     -> brief_spec.json
    2. query_generation  -> queries.json
    3. search            -> search_results.json
    4. candidate_extraction -> candidates.json (+ candidates.csv for review) and
       organisations.json (+ organisations.csv): organisations found by company discovery
    5. organisation_people -> org_people_queries.json, org_people_search_results.json;
       the people found at those organisations are added to candidates.json / .csv
    6. profile_lookup    -> profile_lookup.json: the LinkedIn profile of each person named
       on a web page (one Google query each, accepted on name + organisation); found
       profiles are added to candidates.json and linked to the page record
    7. entity_resolution -> resolved_candidates.json: canonical identities, one merged
       record per person per group
    8. verification      -> verification.json: deterministic eligible / ineligible / needs_review
    9. ranking           -> final_results.json (+ final_results.md, final_results.csv,
       verification.csv): up to MAX_EXPERTS_PER_GROUP experts per group with reasons

Searching stops per group once it has TARGET_CANDIDATES_PER_GROUP qualified
candidates (``adaptive``): the search stage holds back Exa people queries, which
run after extraction only for groups Google left short (their results are added
to search_results.json), and stage 5 runs in batches of organisations.

A run can start at any stage from a saved (e.g. human-reviewed) artifact:
the raw brief, a brief_spec.json, a queries.json, a search_results.json, an
organisations.json or a candidates.json (whose folder must also hold the matching
brief_spec.json, and for organisations.json also candidates.json). Editing organisations.json (``include``)
before stage 5 controls which organisations are searched. It can stop after any later stage. Every
run also writes run_metadata.json (timings, token usage, search usage,
estimated cost, with a time and cost summary per API and per stage at the top),
including runs that fail part-way. File names and loaders are in ``artifacts``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ..adaptive import RoundContext, deepen, first_round, top_up
from ..brief.parser import STAGE as BRIEF_STAGE
from ..brief.parser import parse_brief_file
from ..config import Settings
from ..extraction.candidate_extractor import STAGE as EXTRACTION_STAGE
from ..extraction.candidate_extractor import extract_candidates, organisation_set
from ..llm import AnthropicLLM, StructuredLLM
from ..metrics import RunTracker
from ..models import (
    BriefSpec,
    CandidateSet,
    FinalResults,
    GeneratorInfo,
    QueryPlan,
    ResolvedSet,
    RunMetadata,
    SearchResults,
    VerificationSet,
)
from ..people_search.organisations import STAGE as ORG_PEOPLE_STAGE
from ..people_search.organisations import organisations_by_group, run_organisation_people
from ..people_search.profile_lookup import STAGE as PROFILE_LOOKUP_STAGE
from ..people_search.profile_lookup import merge_lookups, run_profile_lookup
from ..ranking.assessor import STAGE as RANKING_STAGE
from ..ranking.assessor import Assessor, cache_from
from ..ranking.ranker import rank
from ..reporting.progress import (
    duration,
    money,
    report_candidates,
    report_profile_lookup,
    report_queries,
    report_ranking,
    report_resolution,
    report_saved,
    report_spec,
    report_verification,
    say,
)
from ..reporting.review_sheets import write_review_files
from ..reporting.shortlist_files import write_final_files
from ..resolution.identity import STAGE as RESOLUTION_STAGE
from ..resolution.identity import resolve
from ..search.query_generator import STAGE as QUERY_STAGE
from ..search.query_generator import generate_queries
from ..search.runner import STAGE as SEARCH_STAGE
from ..search.runner import SearchClient, run_searches
from ..search.site_search import make_site_search
from ..verification.eligibility import STAGE as VERIFICATION_STAGE
from ..verification.eligibility import verify_all
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
    SEARCH_RESULTS_FILE,
    VERIFICATION_FILE,
    copy_if_elsewhere,
    load_profile_lookups,
    previous_final,
    write_json,
    write_metadata,
)
from .start import STAGES, load_start, step

log = logging.getLogger(__name__)



@dataclass
class PipelineResult:
    spec: BriefSpec
    queries: QueryPlan | None
    search: SearchResults | None
    candidates: CandidateSet | None
    metadata: RunMetadata
    output_dir: Path
    resolved: ResolvedSet | None = None
    verification: VerificationSet | None = None
    final: FinalResults | None = None


def run(
    output_dir: Path,
    *,
    settings: Settings,
    brief_path: Path | None = None,
    spec_path: Path | None = None,
    queries_path: Path | None = None,
    search_path: Path | None = None,
    organisations_path: Path | None = None,
    candidates_path: Path | None = None,
    run_date: date | None = None,
    stop_after: str = STAGES[-1],
    llm: StructuredLLM | None = None,
    search_clients: dict[str, SearchClient] | None = None,
) -> PipelineResult:
    """Run from a raw brief, a saved spec, queries, search results, organisations or candidates (one).

    With a saved spec or queries, the spec's own run_date is used, because its
    date windows were computed from it.
    """
    start = load_start(
        brief_path=brief_path, spec_path=spec_path, queries_path=queries_path, search_path=search_path,
        organisations_path=organisations_path, candidates_path=candidates_path, run_date=run_date,
        stop_after=stop_after,
    )
    first, brief_id, run_date, spec_path = start.first, start.brief_id, start.run_date, start.spec_path
    spec, queries, search = start.spec, start.queries, start.search
    candidates, orgs = start.candidates, start.organisations
    resolved: ResolvedSet | None = None
    verification: VerificationSet | None = None
    final: FinalResults | None = None

    tracker = RunTracker(brief_id, run_date, settings.llm_pricing)
    if llm is None and start.needs_llm(stop_after):
        llm = AnthropicLLM(settings, tracker=tracker)
    output_dir.mkdir(parents=True, exist_ok=True)
    last_stage = first

    def should_run(name: str) -> bool:
        return start.runs(name, stop_after)

    def stage(name: str):
        return tracker.stage(name, step(name))

    say("Run %s: brief %s, stages %s -> %s, output %s", tracker.meta.run_id, brief_id, first, stop_after, output_dir)
    if start.source is not None:
        say("  resuming from saved %s", start.source.name)
    try:
        if should_run(BRIEF_STAGE):
            with stage(BRIEF_STAGE):
                spec = parse_brief_file(
                    brief_path,
                    llm=llm,
                    run_date=run_date,
                    parser_model=settings.llm_model,
                    parser_effort=settings.llm_effort,
                )
            write_json(output_dir / BRIEF_SPEC_FILE, spec.model_dump_json(indent=2))
            report_spec(spec)
            report_saved(BRIEF_SPEC_FILE)
        else:
            copy_if_elsewhere(output_dir / BRIEF_SPEC_FILE, spec_path, spec.model_dump_json(indent=2))

        if should_run(QUERY_STAGE):
            with stage(QUERY_STAGE):
                queries = generate_queries(
                    spec,
                    llm=llm,
                    n=settings.queries_per_provider,
                    xray_n=settings.xray_queries_per_group,
                    site_search=make_site_search(settings, search_clients, tracker),
                    generator=GeneratorInfo(
                        model=settings.llm_model,
                        effort=settings.llm_effort,
                        generated_at=datetime.now(timezone.utc),
                    ),
                )
            write_json(output_dir / QUERIES_FILE, queries.model_dump_json(indent=2))
            report_queries(queries)
            report_saved(QUERIES_FILE)
            last_stage = QUERY_STAGE
        elif queries_path:
            copy_if_elsewhere(output_dir / QUERIES_FILE, queries_path, queries.model_dump_json(indent=2))

        if should_run(SEARCH_STAGE):
            with stage(SEARCH_STAGE):
                search = run_searches(
                    first_round(queries, settings), settings.search, clients=search_clients, tracker=tracker
                )
            write_json(output_dir / SEARCH_RESULTS_FILE, search.model_dump_json(indent=2))
            report_saved(SEARCH_RESULTS_FILE)
            last_stage = SEARCH_STAGE
        elif search_path:
            copy_if_elsewhere(
                output_dir / SEARCH_RESULTS_FILE, search_path, search.model_dump_json(indent=2)
            )

        # One assessor for the run: verdicts made while searching (to stop when a group has
        # enough qualified people) are reused by ranking; verdicts saved by an earlier run in
        # this folder are reused when the dossier is unchanged.
        assessor = (
            Assessor(spec, llm, settings, cache=cache_from(previous_final(output_dir)))
            if llm is not None and spec is not None
            else None
        )
        rounds = RoundContext(
            spec=spec,
            settings=settings,
            llm=llm,
            tracker=tracker,
            clients=search_clients,
            assessor=assessor if settings.qualify_with_llm else None,
        )
        if should_run(EXTRACTION_STAGE):
            with stage(EXTRACTION_STAGE):
                candidates = extract_candidates(
                    spec,
                    search,
                    llm=llm,
                    settings=settings,
                    tracker=tracker,
                    extractor=rounds.extractor(),
                    gate=rounds.gate(),
                )
                if queries is not None:
                    # Groups still short: later Google pages of their X-rays, then Exa people queries.
                    before = len(search.runs)
                    search, candidates = deepen(rounds, queries, search, candidates)
                    search, candidates = top_up(rounds, queries, search, candidates)
                    if len(search.runs) > before:
                        write_json(output_dir / SEARCH_RESULTS_FILE, search.model_dump_json(indent=2))
            orgs = organisation_set(spec, candidates)
            write_json(output_dir / CANDIDATES_FILE, candidates.model_dump_json(indent=2))
            write_json(output_dir / ORGANISATIONS_FILE, orgs.model_dump_json(indent=2))
            review = write_review_files(spec, candidates, output_dir)
            for path in review:
                log.info("wrote %s", path)
            report_candidates(candidates)
            report_saved(CANDIDATES_FILE, ORGANISATIONS_FILE, *(p.name for p in review))
            last_stage = EXTRACTION_STAGE

        if should_run(ORG_PEOPLE_STAGE):
            with stage(ORG_PEOPLE_STAGE):
                # Reviewed include flags from organisations.json decide who is searched and matched.
                candidates = candidates.model_copy(update={"organisations": orgs.organisations})
                org_plan, org_search, candidates = run_organisation_people(rounds, queries, orgs, candidates)
                if org_search is not None:
                    write_json(output_dir / ORG_PEOPLE_SEARCH_FILE, org_search.model_dump_json(indent=2))
                write_json(output_dir / ORG_PEOPLE_QUERIES_FILE, org_plan.model_dump_json(indent=2))
            if organisations_path:
                copy_if_elsewhere(
                    output_dir / ORGANISATIONS_FILE, organisations_path, orgs.model_dump_json(indent=2)
                )
            write_json(output_dir / CANDIDATES_FILE, candidates.model_dump_json(indent=2))
            review = write_review_files(spec, candidates, output_dir)
            for path in review:
                log.info("wrote %s", path)
            report_candidates(candidates)
            report_saved(ORG_PEOPLE_QUERIES_FILE, CANDIDATES_FILE, *(p.name for p in review))
            last_stage = ORG_PEOPLE_STAGE
        elif candidates_path:
            copy_if_elsewhere(
                output_dir / CANDIDATES_FILE, candidates_path, candidates.model_dump_json(indent=2)
            )

        if should_run(PROFILE_LOOKUP_STAGE):
            with stage(PROFILE_LOOKUP_STAGE):
                lookups, candidates = run_profile_lookup(
                    rounds, candidates, organisations_by_group(spec, orgs or organisation_set(spec, candidates)),
                )
            tracker.meta.counts[PROFILE_LOOKUP_STAGE] = lookups.counts()
            report_profile_lookup(lookups)
            if lookups.lookups:  # found profiles and lookup outcomes are recorded on the candidates
                # Add to the lookups of earlier runs (people already looked up are skipped,
                # so an unchanged re-run finds nothing new and leaves the file as it is).
                merged = merge_lookups(load_profile_lookups(output_dir / PROFILE_LOOKUP_FILE), lookups)
                write_json(output_dir / PROFILE_LOOKUP_FILE, merged.model_dump_json(indent=2))
                write_json(output_dir / CANDIDATES_FILE, candidates.model_dump_json(indent=2))
                report_saved(PROFILE_LOOKUP_FILE, CANDIDATES_FILE,
                             *(p.name for p in write_review_files(spec, candidates, output_dir)))
            last_stage = PROFILE_LOOKUP_STAGE

        if should_run(RESOLUTION_STAGE):
            with stage(RESOLUTION_STAGE):
                resolved = resolve(
                    spec,
                    candidates,
                    llm=llm,
                    max_llm_pairs=settings.max_identity_llm_pairs,
                    llm_workers=settings.extraction.llm_workers,
                )
            tracker.meta.counts[RESOLUTION_STAGE] = resolved.stats.model_dump()
            write_json(output_dir / RESOLVED_FILE, resolved.model_dump_json(indent=2))
            report_resolution(resolved)
            report_saved(RESOLVED_FILE)
            last_stage = RESOLUTION_STAGE

        if should_run(VERIFICATION_STAGE):
            with stage(VERIFICATION_STAGE):
                verification = verify_all(spec, resolved)
            tracker.meta.counts[VERIFICATION_STAGE] = _decision_counts(verification)
            write_json(output_dir / VERIFICATION_FILE, verification.model_dump_json(indent=2))
            report_verification(verification)
            report_saved(VERIFICATION_FILE)
            last_stage = VERIFICATION_STAGE

        if should_run(RANKING_STAGE):
            with stage(RANKING_STAGE):
                final = rank(spec, resolved, verification, llm=llm, settings=settings, assessor=assessor)
            tracker.meta.counts[RANKING_STAGE] = {
                "llm_assessed": assessor.asked if assessor else 0,
                "verdicts_reused": assessor.reused if assessor else 0,
                "assessed": len(final.assessments),
                "rejected_by_ranking": sum(g.rejected_by_ranking for g in final.groups),
                "unclear": sum(g.unclear for g in final.groups),
                "returned": sum(len(g.experts) for g in final.groups),
            }
            write_json(output_dir / FINAL_RESULTS_FILE, final.model_dump_json(indent=2))
            exported = write_final_files(final, resolved, verification, output_dir)
            for path in exported:
                log.info("wrote %s", path)
            report_ranking(final, assessor.asked if assessor else 0, assessor.reused if assessor else 0)
            report_saved(FINAL_RESULTS_FILE, *(p.name for p in exported))
            last_stage = RANKING_STAGE
    except Exception as exc:
        tracker.meta.errors.append(f"{type(exc).__name__}: {exc}")
        meta = tracker.finish()
        write_metadata(output_dir, meta)
        say("Run failed after %s (spent %s): %s", duration(meta.duration_s), money(tracker.cost_so_far()),
            type(exc).__name__)
        raise

    metadata = tracker.finish(stopped_after=last_stage)
    write_metadata(output_dir, metadata)
    say("Finished after %s in %s: total %s (LLM %s, search %s)", last_stage, duration(metadata.duration_s),
        money(tracker.cost_so_far()), money(metadata.estimated_llm_cost_usd if metadata.llm_calls else 0.0),
        money(metadata.estimated_search_cost_usd if metadata.search_usage else 0.0))
    return PipelineResult(
        spec=spec,
        queries=queries,
        search=search,
        candidates=candidates,
        metadata=metadata,
        output_dir=output_dir,
        resolved=resolved,
        verification=verification,
        final=final,
    )


def _decision_counts(verification: VerificationSet) -> dict[str, int]:
    counts: dict[str, int] = {"eligible": 0, "ineligible": 0, "needs_review": 0}
    for v in verification.decisions:
        counts[v.decision] += 1
        for r in v.reasons() if v.decision != "eligible" else []:
            key = f"{v.decision}:{r.rule}"
            counts[key] = counts.get(key, 0) + 1
    return counts



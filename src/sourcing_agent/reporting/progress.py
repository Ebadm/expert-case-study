"""Console progress: where the run is and the key numbers of each stage.

Lines go to the ``sourcing_agent.progress`` logger, which the CLI shows by default
(``--quiet`` hides it). ``--verbose`` adds every module's detail logs on top (each
LLM call, search query and file written). Stage start/end lines with timings and
cost come from ``RunTracker.stage``; this module formats the per-stage stats.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import (
        BriefSpec,
        CandidateSet,
        FinalResults,
        QueryPlan,
        ResolvedSet,
        SearchResults,
        VerificationSet,
    )

progress = logging.getLogger("sourcing_agent.progress")


def say(msg: str, *args: object) -> None:
    progress.info(msg, *args)


def money(value: float | None) -> str:
    """$2.33; under a dollar three decimals ($0.073), so small stage costs stay visible."""
    if value is None:
        return "$?"
    if value == 0:
        return "$0.00"
    return f"${value:.2f}" if value >= 1 else f"${value:.3f}"


def duration(seconds: float) -> str:
    """12.3s, 5m 10s, 1h 02m 03s."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, sec = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def report_spec(spec: BriefSpec) -> None:
    target = spec.target_organisation.name if spec.target_organisation else "none"
    say("  brief %s: target %s, %d groups, %d exclusions, %d ambiguities, %d warnings",
        spec.brief_id, target, len(spec.groups), len(spec.excluded_organisations),
        len(spec.ambiguities), len(spec.validation_warnings))
    for g in spec.groups:
        orgs = len(g.eligible_organisations) or "discover"
        say("    %s: status=%s, organisations=%s, %d hard requirements",
            g.group_id, g.required_employment_status, orgs, len(g.hard_constraints))


def report_queries(plan: QueryPlan) -> None:
    for g in plan.groups:
        by = Counter(f"{q.provider} {q.query_type}" for q in g.queries)
        seniority = f", seniority words: {', '.join(g.title_keywords)}" if g.title_keywords else ""
        say("    %s (%s): %d queries [%s]%s", g.group_id, g.mode, len(g.queries),
            ", ".join(f"{k} {v}" for k, v in sorted(by.items())), seniority)
    if plan.validation_warnings:
        say("    %d query warnings (see queries.json)", len(plan.validation_warnings))


def report_search(results: SearchResults) -> None:
    """One line per call of ``run_searches``: queries, hits and failures per provider."""
    parts = []
    for provider in dict.fromkeys(r.provider for r in results.runs):
        runs = [r for r in results.runs if r.provider == provider]
        hits = sum(r.results for r in runs)
        failed = sum(1 for r in runs if r.errors)
        parts.append(f"{provider} {len(runs)} queries / {sum(r.requests for r in runs)} requests -> {hits} hits"
                     + (f", {failed} failed" if failed else ""))
    profiles = len({h.canonical_url for h in results.hits if h.canonical_url.startswith("https://linkedin.com/in/")})
    say("    searched: %s; %d unique URLs (%d LinkedIn profiles)", "; ".join(parts) or "nothing",
        len({h.canonical_url for h in results.hits}), profiles)


def report_candidates(cs: CandidateSet) -> None:
    for r in cs.summary:
        orgs = f", {r.organisations} organisations found" if r.organisations else ""
        say("    %s: %d candidates, %d at a relevant organisation, %d with fitting dates%s",
            r.group_id, r.candidates, r.with_matched_position, r.useful, orgs)
    e = cs.enrichment
    if e.requested:
        say("    Exa /contents: %d/%d pages read, %d failed, %s", e.succeeded, e.requested, e.failed, money(e.cost_usd))


def report_profile_lookup(lookups) -> None:
    for gid in dict.fromkeys(x.group_id for x in lookups.lookups):
        n = Counter(x.outcome for x in lookups.lookups if x.group_id == gid)
        say("    %s: %d looked up -> %d profiles found, %d ambiguous (two or more matches, none taken), "
            "%d not found%s", gid, sum(n.values()), n["found"], n["ambiguous"], n["not_found"],
            f", {n['error']} failed" if n["error"] else "")


def report_resolution(resolved: ResolvedSet) -> None:
    st = resolved.stats
    say("    %d records -> %d people (%d duplicates merged, %d people in several groups, "
        "%d/%d ambiguous pairs merged by the LLM)", st.records_in, st.identities, st.merged_records,
        st.cross_group_identities, st.llm_merges, st.ambiguous_pairs)


def report_verification(verification: VerificationSet) -> None:
    for gid in dict.fromkeys(v.group_id for v in verification.decisions):
        mine = [v for v in verification.decisions if v.group_id == gid]
        decisions = Counter(v.decision for v in mine)
        why = Counter(r.rule for v in mine if v.decision == "ineligible" for r in v.reasons())
        top = ", ".join(f"{rule} {n}" for rule, n in why.most_common(3))
        say("    %s: %d eligible, %d review, %d ineligible%s", gid, decisions["eligible"],
            decisions["needs_review"], decisions["ineligible"], f" (top reasons: {top})" if top else "")


def report_ranking(final: FinalResults, asked: int, reused: int) -> None:
    for g in final.groups:
        say("    %s: %d experts returned (max %d), %d unclear (best %d listed for review), %d rejected",
            g.group_id, len(g.experts), final.max_per_group, g.unclear, len(g.for_review), g.rejected_by_ranking)
    say("    LLM verdicts this run: %d new, %d reused (incl. those made while searching)", asked, reused)


def report_saved(*names: str) -> None:
    say("    saved %s", ", ".join(names))

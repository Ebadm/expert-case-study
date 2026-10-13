"""Search only as much as each group needs (ARCHITECTURE.md §4.4-4.6).

Ranking later picks up to 20 experts per group, so once a group has
``TARGET_CANDIDATES_PER_GROUP`` qualified candidates (they pass verification and the
LLM judges that they meet the group's hard requirements, ``RoundContext.gate``), more
searching only adds cost. Without the LLM gate (``QUALIFY_WITH_LLM=false``) candidates
whose dates fit the group's rule count instead. Searches that may not be needed run as
conditional rounds: each round is searched, extracted and merged before the next
decision.

- Exa people queries are a top-up after Google. On the saved runs Google
  alone gave Tungsten competitors 73 useful candidates, and Exa added 177 more
  that nobody needed. Where Google was thin, Exa still mattered: Tungsten former
  employees (+4) and Polish infrastructure planners (21 of 29 only from Exa).
- Stage 5 searches discovered organisations in batches, best-evidenced first,
  and Exa only once Google's batches are used up (``organisation_people``).
- LinkedIn X-rays fetch Google page 1 only; pages 2-3 are fetched (``deepen``) only for
  groups that read everything and are still short. On the run of 2026-10-11, extraction
  read ranks 1-12 of the former-employee X-rays and 1-4 of the competitor ones, so about
  half of the 105 page requests were never read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .config import Settings
from .extraction.candidate_extractor import extract_candidates, merge_candidate_sets
from .llm import StructuredLLM
from .metrics import RunTracker
from .models import (
    BriefSpec,
    CandidateSet,
    DiscoveredOrganisation,
    ExpertGroup,
    GeneratorInfo,
    Organisation,
    QueryPlan,
    SearchQuery,
    SearchResults,
)
from .ranking.assessor import Assessor
from .reporting.progress import say
from .search.runner import SearchClient, run_searches, summarise
from .verification.employment import count_useful, relevant_organisations

log = logging.getLogger(__name__)


@dataclass
class RoundContext:
    spec: BriefSpec
    settings: Settings
    llm: StructuredLLM
    tracker: RunTracker | None = None
    clients: dict[str, SearchClient] | None = None
    # When set, "enough" means candidates that pass verification and the LLM's group check
    # (the final list's test); otherwise candidates whose dates fit.
    assessor: Assessor | None = None

    def gate(self, organisations: list[DiscoveredOrganisation] = ()):
        if self.assessor is None:
            return None
        return lambda group, candidates: self.assessor.qualified(group, candidates, list(organisations))

    def extractor(self) -> GeneratorInfo:
        return GeneratorInfo(model=self.settings.llm_model, effort=self.settings.llm_effort,
                             generated_at=datetime.now(timezone.utc))


def is_top_up(q: SearchQuery) -> bool:
    """Exa people queries run only for groups Google left short of the target."""
    return q.provider == "exa" and q.query_type == "people"


def sub_plan(plan: QueryPlan, keep: Callable[[SearchQuery], bool]) -> QueryPlan:
    return plan.model_copy(update={"groups": [
        g.model_copy(update={"queries": [q for q in g.queries if keep(q)]}) for g in plan.groups
    ]})


def is_xray(q: SearchQuery) -> bool:
    return q.provider == "serper" and q.query_type == "people"


def first_page_only(plan: QueryPlan, settings: Settings) -> QueryPlan:
    """With a target, LinkedIn X-rays fetch Google page 1 only; ``deepen`` fetches the rest if needed."""
    if not settings.target_candidates_per_group or settings.search.serper_people_max_pages <= 1:
        return plan
    return plan.model_copy(update={"groups": [
        g.model_copy(update={"queries": [q.model_copy(update={"max_pages": 1}) if is_xray(q) else q
                                         for q in g.queries]}) for g in plan.groups
    ]})


def first_round(plan: QueryPlan, settings: Settings) -> QueryPlan:
    """The queries the search stage runs: all of them without a target, else all but the top-up,
    with LinkedIn X-rays on page 1 only."""
    if not settings.target_candidates_per_group:
        return plan
    return first_page_only(sub_plan(plan, lambda q: not is_top_up(q)), settings)


def merge_search_results(a: SearchResults | None, b: SearchResults) -> SearchResults:
    if a is None:
        return b
    runs, hits = [*a.runs, *b.runs], [*a.hits, *b.hits]
    return a.model_copy(update={"runs": runs, "hits": hits, "summary": summarise(runs, hits)})


def qualified_count(spec: BriefSpec, group: ExpertGroup, cs: CandidateSet,
                 extra: dict[str, list[Organisation]] | None = None, assessor: Assessor | None = None) -> int:
    """Qualified candidates (with an assessor) or candidates whose dates fit (without)."""
    if assessor is not None:
        return assessor.qualified(group, cs.candidates, cs.organisations)
    discovered = [o.name for o in cs.organisations if o.group_id == group.group_id and o.include]
    discovered += [o.name for o in (extra or {}).get(group.group_id, [])]
    return count_useful(cs.candidates, group, relevant_organisations(spec, group, discovered))


def search_round(
    ctx: RoundContext, plan: QueryPlan, candidates: CandidateSet,
    extra: dict[str, list[Organisation]] | None = None,
) -> tuple[SearchResults, CandidateSet]:
    """Run ``plan``, extract its results (counting ``candidates`` toward each target) and merge."""
    search = run_searches(plan, ctx.settings.search, clients=ctx.clients, tracker=ctx.tracker)
    found = extract_candidates(
        ctx.spec, search, llm=ctx.llm, settings=ctx.settings, tracker=ctx.tracker, extractor=ctx.extractor(),
        extra_organisations=extra, base_candidates=candidates.candidates,
        gate=ctx.gate(candidates.organisations),
    )
    return search, merge_candidate_sets(ctx.spec, candidates, found)


def top_up(
    ctx: RoundContext, plan: QueryPlan, search: SearchResults, candidates: CandidateSet,
) -> tuple[SearchResults, CandidateSet]:
    """Run the Exa people queries (not already in ``search``) of groups still short of the target."""
    target = ctx.settings.target_candidates_per_group
    done = {r.query_id for r in search.runs}
    run: set[str] = set()
    notes: list[str] = []
    for g in plan.groups:
        pending = [q for q in g.queries if is_top_up(q) and q.query_id not in done]
        if not pending:
            continue
        have = qualified_count(ctx.spec, ctx.spec.group(g.group_id), candidates, assessor=ctx.assessor)
        if target and have >= target:
            notes.append(f"{g.group_id}: {have} qualified candidates from Google (target {target}); "
                         f"{len(pending)} Exa people queries not run")
            continue
        say("    %s: %d qualified candidates (target %d), running %d Exa people queries",
            g.group_id, have, target, len(pending))
        run |= {q.query_id for q in pending}
    candidates = candidates.model_copy(update={"warnings": candidates.warnings + notes})
    for n in notes:
        say("    %s", n)
    if not run:
        return search, candidates
    extra, candidates = search_round(ctx, sub_plan(plan, lambda q: q.query_id in run), candidates)
    return merge_search_results(search, extra), candidates


def deeper_pages(plan: QueryPlan, search: SearchResults, max_pages: int) -> list[SearchQuery]:
    """The next Google page of every X-ray whose last page was full, up to ``max_pages``."""
    queries = {q.query_id: q for q in plan.queries}
    reached: dict[str, tuple[int, str]] = {}  # X-ray -> (last page fetched, why it stopped)
    for r in search.runs:
        base, _, page = r.query_id.partition("+p")
        last = (int(page) if page else 1) + r.requests - 1
        if base not in reached or last > reached[base][0]:
            reached[base] = (last, r.stop_reason)
    out: list[SearchQuery] = []
    for base, (last, stop) in reached.items():
        q = queries.get(base)
        if q is None or not is_xray(q) or stop != "max_pages" or last + 1 > max_pages:
            continue
        out.append(q.model_copy(update={"query_id": f"{base}+p{last + 1}", "first_page": last + 1, "max_pages": 1}))
    return out


def deepen(
    ctx: RoundContext, plan: QueryPlan, search: SearchResults, candidates: CandidateSet,
    extra: dict[str, list[Organisation]] | None = None,
) -> tuple[SearchResults, CandidateSet]:
    """Fetch later Google pages of the X-rays of groups still short of the target, one page at a time.

    A group gets here only after reading every hit it had, so being short means it ran out.
    Profiles already found on an earlier page are not read again.
    """
    target = ctx.settings.target_candidates_per_group
    while target:
        pending: dict[str, list[SearchQuery]] = {}
        for q in deeper_pages(plan, search, ctx.settings.search.serper_people_max_pages):
            pending.setdefault(q.group_id, []).append(q)
        run: list[SearchQuery] = []
        for gid, qs in pending.items():
            have = qualified_count(ctx.spec, ctx.spec.group(gid), candidates, extra, assessor=ctx.assessor)
            if have >= target:
                continue
            say("    %s: %d qualified candidates (target %d), fetching Google page %d of %d LinkedIn X-rays",
                gid, have, target, qs[0].first_page, len(qs))
            run += qs
        if not run:
            break
        ids = {q.group_id for q in run}
        more_plan = plan.model_copy(update={"groups": [
            g.model_copy(update={"queries": [q for q in run if q.group_id == g.group_id]})
            for g in plan.groups if g.group_id in ids
        ]})
        more = run_searches(more_plan, ctx.settings.search, clients=ctx.clients, tracker=ctx.tracker)
        seen = {(h.group_id, h.canonical_url) for h in search.hits}
        seen |= {(c.group_id, c.canonical_url) for c in candidates.candidates if c.canonical_url}
        fresh = more.model_copy(update={"hits": [h for h in more.hits if (h.group_id, h.canonical_url) not in seen]})
        found = extract_candidates(
            ctx.spec, fresh, llm=ctx.llm, settings=ctx.settings, tracker=ctx.tracker, extractor=ctx.extractor(),
            extra_organisations=extra, base_candidates=candidates.candidates,
            gate=ctx.gate(candidates.organisations),
        )
        search, candidates = merge_search_results(search, more), merge_candidate_sets(ctx.spec, candidates, found)
    return search, candidates

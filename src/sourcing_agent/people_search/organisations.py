"""People at organisations found by company discovery (stage 5).

Groups without named organisations (e.g. the target's customers) first discover
organisations (stages 2-4, saved as organisations.json). This stage turns the
included organisations into people queries (one Google X-ray per organisation,
best-evidenced first; Exa only for groups not defined by the target), runs them, and extracts
candidates whose positions are matched against those organisations. For
customer groups the queries require the target's names, and each candidate
records whether its profile names the target (``target_mentioned``): the
evidence of hands-on use the brief asks for.

The queries run in batches of ``ORG_BATCH`` organisations and stop once the group
has ``TARGET_CANDIDATES_PER_GROUP`` qualified candidates. Each X-ray fetches Google
page 1 only; a group still short after every batch gets the later pages of those
X-rays (``adaptive.deepen``), and Exa runs only after that. On a saved Polish run the
first 10 terminal operators gave 99 candidates whose dates fit, and the other 30
searches only added more.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..adaptive import (
    RoundContext,
    deepen,
    first_page_only,
    merge_search_results,
    qualified_count,
    search_round,
    sub_plan,
)
from ..config import Settings
from ..models import (
    BriefSpec,
    CandidateSet,
    GeneratorInfo,
    GroupQueryPlan,
    Organisation,
    OrganisationSet,
    QueryPlan,
    SearchResults,
    SearchTermsDraft,
)
from ..reporting.progress import say
from ..search.people_plan import build_organisation_people_queries

log = logging.getLogger(__name__)

STAGE = "organisation_people"
# Organisations searched per round: on the saved runs ~2-6 useful people per
# organisation, so one batch of 10 often reaches the target.
ORG_BATCH = 10


def organisations_by_group(spec: BriefSpec, orgs: OrganisationSet) -> dict[str, list[Organisation]]:
    """Included organisations per discovery group, best-evidenced first."""
    return {
        g.group_id: [Organisation(name=o.name, aliases=[]) for o in orgs.for_group(g.group_id)]
        for g in spec.groups if not g.eligible_organisations and orgs.for_group(g.group_id)
    }


def plan_organisation_people(
    spec: BriefSpec, plan: QueryPlan | None, orgs: OrganisationSet, settings: Settings,
) -> QueryPlan:
    groups: list[GroupQueryPlan] = []
    for gid, found in organisations_by_group(spec, orgs).items():
        group = spec.group(gid)
        known = {g.group_id: g for g in plan.groups} if plan else {}
        terms = SearchTermsDraft(
            role_keywords=known[gid].role_keywords if gid in known else [],
            topic_keywords=known[gid].topic_keywords if gid in known else [],
            title_keywords=known[gid].title_keywords if gid in known else [],
        )
        queries = build_organisation_people_queries(
            spec, group, found, terms,
            max_xray=settings.org_people_queries_per_group, max_exa=settings.queries_per_provider,
        )
        groups.append(GroupQueryPlan(
            group_id=gid, mode="people", role_keywords=terms.role_keywords,
            topic_keywords=terms.topic_keywords, title_keywords=terms.title_keywords, queries=queries,
        ))
    return QueryPlan(
        brief_id=spec.brief_id, source_sha256=spec.source.sha256, spec_parsed_at=spec.parser.parsed_at,
        generator=GeneratorInfo(model="templates", effort="n/a", generated_at=datetime.now(timezone.utc)),
        queries_per_provider=settings.queries_per_provider,
        xray_queries_per_group=settings.org_people_queries_per_group, groups=groups,
    )


def run_organisation_people(
    ctx: RoundContext, plan: QueryPlan | None, orgs: OrganisationSet, candidates: CandidateSet,
) -> tuple[QueryPlan, SearchResults | None, CandidateSet]:
    """Plan every organisation's queries, then run them batch by batch until each group has enough.

    Returns the full plan (org_people_queries.json), what was run and the merged candidates.
    """
    spec, target = ctx.spec, ctx.settings.target_candidates_per_group
    org_plan = plan_organisation_people(spec, plan, orgs, ctx.settings)
    extra = organisations_by_group(spec, orgs)
    search: SearchResults | None = None
    for g in org_plan.groups:
        group = spec.group(g.group_id)
        google = [q for q in g.queries if q.provider != "exa"]
        exa = [q for q in g.queries if q.provider == "exa"]
        size = ORG_BATCH if target else max(1, len(google))
        rounds = [google[i:i + size] for i in range(0, len(google), size)] + ([exa] if exa else [])
        for i, batch in enumerate(rounds):
            have = qualified_count(spec, group, candidates, extra, assessor=ctx.assessor)
            if target and have >= target:
                skipped = sum(len(b) for b in rounds[i:])
                note = f"{g.group_id}: {have} qualified candidates (target {target}); {skipped} people queries not run"
                say("    %s", note)
                candidates = candidates.model_copy(update={"warnings": candidates.warnings + [note]})
                break
            if batch is exa and search is not None:
                # Every organisation searched and still short: later Google pages before Exa.
                group_plan = sub_plan(org_plan, lambda q, gid=g.group_id: q.group_id == gid)
                search, candidates = deepen(ctx, group_plan, search, candidates, extra)
                have = qualified_count(spec, group, candidates, extra, assessor=ctx.assessor)
                if target and have >= target:
                    note = f"{g.group_id}: {have} qualified candidates (target {target}); {len(exa)} people queries not run"
                    say("    %s", note)
                    candidates = candidates.model_copy(update={"warnings": candidates.warnings + [note]})
                    break
            ids = {q.query_id for q in batch}
            say("    %s: round %d/%d, %d %s people queries (%d qualified so far, target %d)", g.group_id, i + 1,
                len(rounds), len(batch), batch[0].provider, have, target)
            batch_plan = first_page_only(sub_plan(org_plan, lambda q: q.query_id in ids), ctx.settings)
            found, candidates = search_round(ctx, batch_plan, candidates, extra)
            search = merge_search_results(search, found)
        else:
            if google and search is not None and not exa:
                group_plan = sub_plan(org_plan, lambda q, gid=g.group_id: q.group_id == gid)
                search, candidates = deepen(ctx, group_plan, search, candidates, extra)
    return org_plan, search, candidates

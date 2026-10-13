"""Console summaries printed after a run (stdout): what each stage produced, in tables.
"""

from __future__ import annotations

from pathlib import Path

from ..models import BriefSpec, CandidateSet, FinalResults, QueryPlan, ResolvedSet, RunMetadata, SearchResults


def print_spec_summary(spec: BriefSpec) -> None:
    print(f"\n{spec.title}  [{spec.brief_id}]  run_date={spec.run_date}")
    if spec.target_organisation:
        t = spec.target_organisation
        aliases = f" (aka {', '.join(t.aliases)})" if t.aliases else ""
        print(f"Target: {t.name}{aliases}")
    print(f"Sector: {spec.sector}")

    if spec.excluded_organisations:
        print("\nExclusions:")
        for ex in spec.excluded_organisations:
            names = " / ".join([ex.organisation.name, *ex.organisation.aliases])
            groups = ", ".join(ex.applies_to_group_ids) or "all groups"
            print(f"  - {names}: {ex.scope} ({groups})")

    for g in spec.groups:
        print(f"\n[{g.group_id}] {g.name}  status={g.required_employment_status}  langs={','.join(g.query_languages)}")
        orgs = [o.name for o in g.eligible_organisations] or g.eligible_organisation_types
        print(f"  organisations: {', '.join(orgs)}")
        if g.departure_dates:
            d = g.departure_dates
            print(f"  left between: {d.left_on_or_after or '-'} .. {d.left_on_or_before or '-'}")
        if g.excluded_roles:
            print(f"  excluded roles: {', '.join(g.excluded_roles)}")
        print(f"  hard constraints: {len(g.hard_constraints)}  preferences: {len(g.preferences)}")

    if spec.ambiguities:
        print("\nAmbiguities flagged by parser:")
        for a in spec.ambiguities:
            print(f"  - {a}")
    if spec.validation_warnings:
        print("\nBrief validation warnings:")
        for w in spec.validation_warnings:
            print(f"  ! {w}")


def print_query_summary(plan: QueryPlan) -> None:
    print("\nQueries:")
    for g in plan.groups:
        print(f"\n[{g.group_id}] mode={g.mode}")
        if g.role_keywords or g.topic_keywords:
            print(f"  role keywords: {', '.join(g.role_keywords) or '-'}")
            print(f"  topic keywords: {', '.join(g.topic_keywords) or '-'}")
        for q in g.queries:
            print(f"  {q.query_type:<7} {q.provider:<5} {q.language}  {q.query}")
        if g.mode == "companies_then_people":
            print("  (people queries are built once organisations are discovered)")
    if plan.validation_warnings:
        print("\nQuery validation warnings:")
        for w in plan.validation_warnings:
            print(f"  ! {w}")


def print_search_summary(results: SearchResults, top: int = 3) -> None:
    print("\nSearch results:")
    print(f"  {'group':<32} {'provider':<8} {'type':<8} {'queries':>7} {'hits':>5} {'unique':>6} "
          f"{'linkedin':>8} {'junk':>5} {'errors':>6}")
    for r in results.summary:
        print(f"  {r.group_id:<32} {r.provider:<8} {r.query_type:<8} {r.queries:>7} {r.hits:>5} "
              f"{r.unique_urls:>6} {r.linkedin_profiles:>8} {r.filtered:>5} {r.errors:>6}")
    all_urls = {h.canonical_url for h in results.hits}
    both = {h.canonical_url for h in results.hits if h.provider == "serper"} & {
        h.canonical_url for h in results.hits if h.provider == "exa"}
    print(f"  total: {len(results.hits)} hits, {len(all_urls)} unique URLs, {len(both)} found by both providers")

    for r in results.summary:
        hits = [h for h in results.hits
                if (h.group_id, h.provider, h.query_type) == (r.group_id, r.provider, r.query_type)]
        if hits:
            print(f"\n  [{r.group_id}] {r.provider} {r.query_type}, first {min(top, len(hits))}:")
            for h in hits[:top]:
                print(f"    - {h.title[:110]}")
                print(f"      {h.url}")
    failed = [run for run in results.runs if run.errors]
    if failed:
        print("\nSearch errors:")
        for run in failed:
            print(f"  ! {run.query_id}: {run.errors[0][:200]}")


def print_candidate_summary(cs: CandidateSet, top: int = 3) -> None:
    print("\nCandidates:")
    print(f"  {'group':<32} {'candidates':>10} {'matched':>8} {'useful':>6} {'orgs':>5}  by method")
    for r in cs.summary:
        methods = ", ".join(f"{k}={v}" for k, v in sorted(r.by_method.items()))
        print(f"  {r.group_id:<32} {r.candidates:>10} {r.with_matched_position:>8} {r.useful:>6} "
              f"{r.organisations:>5}  {methods}")
    stops = [w for w in cs.warnings if w.endswith(("not run", "not read"))]
    if stops:
        print("  early stops (group had enough):")
        for w in stops:
            print(f"    - {w}")
    e = cs.enrichment
    cost = f"${e.cost_usd:.4f}" if e.cost_usd is not None else "unknown"
    print(f"  enrichment (Exa /contents): {e.succeeded}/{e.requested} pages, {e.failed} failed, {cost}")
    for r in cs.summary:
        shown = [c for c in cs.for_group(r.group_id) if any(p.matched_organisations for p in c.positions)][:top]
        if shown:
            print(f"\n  [{r.group_id}] examples:")
        for c in shown:
            p = next(p for p in c.positions if p.matched_organisations)
            when = f"{p.start or '?'} -> {'present' if p.is_current else p.end or '?'}"
            print(f"    - {c.name}: {p.title or '?'} @ {p.organisation} ({when}) [{c.extraction_method}]")
    orgs = [o for o in cs.organisations][:8]
    if orgs:
        print("\n  discovered organisations (first 8):")
        for o in orgs:
            print(f"    - [{o.group_id}] {o.name}: {o.relationship[:90]}")
    if cs.warnings:
        print(f"\n  {len(cs.warnings)} extraction warnings (see candidates.json), e.g. {cs.warnings[0][:150]}")
    print("\n  Review sheet: candidates.csv (one row per candidate, likely-eligible first; reviewer columns to fill)")


def print_resolution_summary(resolved: ResolvedSet) -> None:
    st = resolved.stats
    print(f"\nEntity resolution: {st.records_in} records -> {st.identities} people, {st.records_out} records "
          f"after merging ({st.within_group_duplicates} within-group duplicates, {st.cross_group_identities} people "
          f"in several groups, {st.llm_merges}/{st.ambiguous_pairs} ambiguous pairs merged by the LLM)")


def print_final_summary(final: FinalResults, output_dir: Path, top: int = 5) -> None:
    print("\nShortlist:")
    print(f"  {'group':<32} {'candidates':>10} {'eligible':>8} {'review':>6} {'rejected':>8} {'unclear':>7} "
          f"{'returned':>8}")
    for g in final.groups:
        print(f"  {g.group_id:<32} {g.candidates:>10} {g.eligible:>8} {g.needs_review:>6} "
              f"{g.rejected_by_ranking:>8} {g.unclear:>7} {len(g.experts):>8}")
    for g in final.groups:
        if g.experts:
            print(f"\n  [{g.group_id}] top {min(top, len(g.experts))}:")
        for e in g.experts[:top]:
            print(f"    {e.rank:>2}. {e.name} - {e.title or '?'}, {e.company} ({e.status}) score {e.score.total:.0f}")
            print(f"        {e.reason[:150]}")
    print(f"\n  Final list: {output_dir / 'final_results.md'} (also .json, .csv); every decision: verification.csv")


def print_run_summary(meta: RunMetadata, output_dir: Path) -> None:
    """Time and cost: total, per API and per stage (the summary at the top of run_metadata.json)."""
    sm = meta.summary
    if sm is None:
        return
    print(f"\nRun {meta.run_id}: {sm.status}")
    print(f"  Time:  {sm.duration} ({sm.stages})")
    print(f"  Cost:  {sm.total_cost}")
    for api, text in sm.cost_by_api.items():
        print(f"    {api}: {text}")
    if sm.time_and_cost_by_stage:
        print("  By stage:")
        for stage, text in sm.time_and_cost_by_stage.items():
            print(f"    {stage}: {text}")
    print(f"  Details: {output_dir / 'run_metadata.json'}")

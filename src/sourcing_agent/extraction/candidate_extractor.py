"""Candidate extraction stage: ``SearchResults`` -> ``CandidateSet``.

1. People-search hits are grouped by profile (canonical URL); company-discovery
   hits are grouped by page and capped per group (best-ranked first).
2. Each group is read in chunks, best-ranked first (rank 1 of every query, then
   rank 2, ...). Only the chunk being read is enriched: pages/profiles without
   content (Serper/Google only returns snippets) go through Exa ``/contents``;
   Exa search hits already carry content.
3. Profiles -> candidates (``profiles``: structured data first, LLM fallback);
   pages -> quoted people + qualifying organisations (``pages``, Haiku).
4. Early stop (``TARGET_CANDIDATES_PER_GROUP``): a group's profiles stop once it
   has that many qualified candidates. With a ``gate`` (the pipeline's default) a
   candidate counts when it passes verification and the LLM finds it meets the group's
   hard requirements, the same test the final list uses; without one, when its dates
   fit the group's rule. Its discovery
   pages stop once it has ``ORG_PEOPLE_QUERIES_PER_GROUP`` organisations, the
   most stage 5 will search. The rest are never enriched or sent to the LLM.
5. Duplicates within a group are merged on exact keys only (same profile URL,
   or same name + organisation from pages). Fuzzy deduplication, eligibility
   and ranking are later stages: every candidate leaves here "unverified".
"""

from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, Iterator, TypeVar

from ..config import Settings
from ..llm import StructuredLLM
from ..metrics import RunTracker
from ..models import (
    BriefSpec,
    Candidate,
    CandidateSet,
    CandidateSummaryRow,
    DiscoveredOrganisation,
    EnrichmentStats,
    Evidence,
    ExpertGroup,
    GeneratorInfo,
    Organisation,
    OrganisationSet,
    SearchHit,
    SearchResults,
    SearchUsage,
)
from ..reporting.progress import say
from ..search.base import ProviderUnusable
from ..verification.employment import departure_status, relevant_organisations, useful_statuses
from .enrichment import ContentsResult, ExaContents, PageContent
from .orgs import (
    lines_mentioning,
    matching_organisations,
    mentions_any,
    org_key,
    organisations_for_group,
    same_organisation,
)
from .pages import extract_from_page
from .profiles import candidate_from_profile, content_from_hit

log = logging.getLogger(__name__)

STAGE = "candidate_extraction"

T = TypeVar("T")
Key = tuple[str, str]  # (group_id, canonical URL)
# (group, its candidates so far) -> how many would make the final list
Gate = Callable[[ExpertGroup, list[Candidate]], int]


def extract_candidates(
    spec: BriefSpec,
    search: SearchResults,
    *,
    llm: StructuredLLM,
    settings: Settings,
    extractor: GeneratorInfo,
    contents: ExaContents | None = None,
    tracker: RunTracker | None = None,
    extra_organisations: dict[str, list[Organisation]] | None = None,
    base_candidates: list[Candidate] | None = None,
    gate: Gate | None = None,
) -> CandidateSet:
    """``extra_organisations``: per group, organisations found by company discovery, so
    positions there are matched (used by the people search at those organisations).
    ``gate``: counts qualified candidates (see 4. above); without it, date fit counts.
    ``base_candidates``: candidates found in an earlier round, counted toward each
    group's target so a later round stops as soon as the group has enough."""
    cfg = settings.extraction
    target = settings.target_candidates_per_group
    orgs_wanted = settings.org_people_queries_per_group if target else 0
    groups = {g.group_id: g for g in spec.groups}
    extra = extra_organisations or {}
    org_lists = {gid: organisations_for_group(spec, g) + list(extra.get(gid, [])) for gid, g in groups.items()}
    target_org = spec.target_organisation
    target_names = [target_org.name, *target_org.aliases] if target_org else []
    warnings: list[str] = []

    profiles: dict[Key, list[SearchHit]] = defaultdict(list)
    page_hits: dict[Key, list[SearchHit]] = defaultdict(list)
    for h in search.hits:
        if h.group_id not in groups:
            continue
        (profiles if h.query_type == "people" else page_hits)[(h.group_id, h.canonical_url)].append(h)
    pages = _cap_pages(page_hits, cfg.max_pages_per_group, warnings)

    # Content already returned by Exa search; everything else goes to /contents, chunk by chunk.
    known: dict[str, PageContent] = {}
    for h in search.hits:
        c = content_from_hit(h)
        if c is not None and h.canonical_url not in known:
            known[h.canonical_url] = c
    enricher = _Enricher(settings, contents)
    run_order = {r.query_id: i for i, r in enumerate(search.runs)}

    def do_profile(item):
        (gid, canon), hits = item
        content = known.get(canon)
        cand, w = candidate_from_profile(groups[gid], org_lists[gid], hits, content, llm)
        cand.target_mentioned = mentions_any(content.text if content else None, target_names)
        # Keep the lines that name the target as quotes: for customer groups, the evidence of use.
        for line in lines_mentioning(content.text if content else None, target_names) if cand.target_mentioned else []:
            cand.evidence.append(Evidence(
                source_url=content.url, source_type="profile", provider="exa", retrieved_at=datetime.now(timezone.utc),
                claim=f"Profile mentions {target_org.name}", supporting_text=line, evidence_type="quote",
                supports=["target_mention"],
            ))
        return cand, w

    def do_page(item):
        (gid, canon), hits = item
        page = known.get(canon)
        if page is None or not page.text:
            return [], [], [f"{hits[0].url}: no page text, skipped"]
        try:
            people, found, w = extract_from_page(
                spec, groups[gid], org_lists[gid], page, llm,
                hit_ids=[h.hit_id for h in hits], query_ids=list(dict.fromkeys(h.query_id for h in hits)),
                providers=list(dict.fromkeys(h.provider for h in hits)),
            )
            for person in people:
                person.target_mentioned = mentions_any(page.text, target_names)
            return people, found, w
        except ProviderUnusable:
            raise  # out of credits: stop the run
        except Exception as exc:  # one bad page must not sink the stage
            log.exception("page extraction failed for %s", hits[0].url)
            return [], [], [f"{hits[0].url}: extraction failed: {type(exc).__name__}: {exc}"]

    def missing(chunk: list[tuple[Key, list[SearchHit]]]) -> list[str]:
        return [hits[0].url for (_, canon), hits in chunk if canon not in known]

    def do_group(gid: str, pool: ThreadPoolExecutor) -> tuple[list[Candidate], list[DiscoveredOrganisation], list[str]]:
        group = groups[gid]
        out: list[Candidate] = []
        notes: list[str] = []
        # Profiles, until the group has enough qualified candidates.
        relevant = relevant_organisations(spec, group, [o.name for o in extra.get(gid, [])])
        ok = useful_statuses(group)
        earlier = [c for c in base_candidates or [] if c.group_id == gid]

        def count() -> int:
            if gate is not None:
                return gate(group, earlier + out)
            return len({c.candidate_id for c in earlier + out if departure_status(c, group, relevant) in ok})

        mine = sorted(((k, v) for k, v in profiles.items() if k[0] == gid), key=lambda kv: _priority(kv[1], run_order))
        kind = "qualified" if gate is not None else "useful"
        read, have = 0, (count() if target and mine else 0)
        for chunk in _chunks(mine, cfg.batch_size):
            if target and have >= target:
                break
            enricher.fill(known, missing(chunk))
            for cand, w in pool.map(do_profile, chunk):
                out.append(cand)
                notes += w
            read += len(chunk)
            have = count() if target else have
            say("    %s: %d/%d profiles read, %s", gid, read, len(mine),
                f"{have} {kind} (target {target})" if target else f"{len(out)} candidates")
        if read < len(mine):
            notes.append(f"{gid}: {have} {kind} candidates (target {target}) after "
                         f"{read} of {len(mine)} profiles; the other {len(mine) - read} were not read")
            say("    %s: enough %s candidates, %d profiles not read", gid, kind, len(mine) - read)

        # Discovery pages, until the group has as many organisations as stage 5 searches.
        mine_pages = [(k, v) for k, v in pages.items() if k[0] == gid]
        found_here: list[DiscoveredOrganisation] = []
        read = 0
        for chunk in _chunks(mine_pages, cfg.batch_size):
            if orgs_wanted and _distinct(spec, found_here) >= orgs_wanted:
                break
            enricher.fill(known, missing(chunk))
            for people, found, w in pool.map(do_page, chunk):
                out += people
                found_here += found
                notes += w
            read += len(chunk)
            say("    %s: %d/%d discovery pages read, %d organisations, %d people named", gid, read,
                len(mine_pages), _distinct(spec, found_here), sum(1 for c in out if c.extraction_method == "llm_page"))
        if read < len(mine_pages):
            notes.append(f"{gid}: {_distinct(spec, found_here)} organisations (wanted {orgs_wanted}) "
                         f"after {read} of {len(mine_pages)} pages; the rest were not read")
        return out, found_here, notes

    candidates: list[Candidate] = []
    orgs: list[DiscoveredOrganisation] = []
    try:
        # Groups run side by side and share one pool of LLM workers.
        with (ThreadPoolExecutor(max_workers=cfg.llm_workers) as pool,
              ThreadPoolExecutor(max_workers=max(1, len(groups))) as outer):
            for cands, found, notes in outer.map(lambda gid: do_group(gid, pool), list(groups)):
                candidates += cands
                orgs += found
                warnings += notes
    finally:
        enricher.close()
    warnings += enricher.warnings()
    if tracker is not None and enricher.stats.requested:
        tracker.record_search_usage(SearchUsage(
            provider="exa", purpose="contents", requests=enricher.requests, results=enricher.stats.succeeded,
            errors=enricher.stats.failed, cost_usd=enricher.stats.cost_usd,
            cost_source="provider-reported (costDollars)",
        ))

    candidates = _merge_candidates(candidates)
    orgs = _merge_organisations(canonicalise_organisations(spec, orgs))
    _tag_discovered_organisations(spec, candidates, orgs)
    return CandidateSet(
        brief_id=spec.brief_id,
        source_sha256=spec.source.sha256,
        extracted_at=datetime.now(timezone.utc),
        extractor=extractor,
        summary=_summarise(spec, candidates, orgs, extra),
        enrichment=enricher.stats,
        candidates=candidates,
        organisations=orgs,
        warnings=warnings,
    )


class _Enricher:
    """Fetches missing content through Exa ``/contents`` on demand and totals the cost."""

    def __init__(self, settings: Settings, contents: ExaContents | None) -> None:
        self._settings = settings
        self._contents = contents
        self._own = contents is None
        self._unfetched = 0
        self._errors: list[str] = []
        self.requests = 0
        self.stats = EnrichmentStats()
        self._lock = Lock()  # groups enrich from parallel threads

    def fill(self, known: dict[str, PageContent], urls: list[str]) -> None:
        if not urls or not self._settings.extraction.enrich:
            return
        with self._lock:
            if self._contents is None and self._settings.search.exa_api_key:
                self._contents = ExaContents(self._settings.search.exa_api_key,
                                             text_max_chars=self._settings.search.exa_text_max_chars)
            if self._contents is None:
                self._unfetched += len(urls)
                return
        fetched = self._contents.fetch(urls)
        with self._lock:
            self._record(known, fetched)

    def _record(self, known: dict[str, PageContent], fetched: ContentsResult) -> None:
        known.update(fetched.pages)
        cost = self.stats.cost_usd
        self.requests += fetched.requests
        self.stats = EnrichmentStats(
            requested=self.stats.requested + fetched.requested,
            succeeded=self.stats.succeeded + len(fetched.pages),
            failed=self.stats.failed + len(fetched.failed),
            cost_usd=None if cost is None or fetched.cost_usd is None else round(cost + fetched.cost_usd, 6),
        )
        self._errors += fetched.errors

    def warnings(self) -> list[str]:
        unfetched = [f"EXA_API_KEY not set: {self._unfetched} profiles/pages not enriched"] if self._unfetched else []
        return self._errors + unfetched

    def close(self) -> None:
        if self._own and self._contents is not None:
            self._contents.close()


def _priority(hits: list[SearchHit], run_order: dict[str, int]) -> tuple[int, int]:
    """Best rank first, then the earlier query: rank 1 of every query before any rank 2."""
    return min((h.rank, run_order.get(h.query_id, len(run_order))) for h in hits)


def _chunks(items: list[T], size: int) -> Iterator[list[T]]:
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _distinct(spec: BriefSpec, orgs: list[DiscoveredOrganisation]) -> int:
    """Organisations after the same name-variant merging the final list gets."""
    return len(_merge_organisations(canonicalise_organisations(spec, orgs)))


def organisation_set(spec: BriefSpec, cs: CandidateSet) -> OrganisationSet:
    """The organisations found by company discovery, best-evidenced first (organisations.json)."""
    orgs = sorted(cs.organisations, key=lambda o: (o.group_id, -o.sources, o.name.lower()))
    return OrganisationSet(brief_id=spec.brief_id, source_sha256=spec.source.sha256,
                           created_at=datetime.now(timezone.utc), organisations=orgs)


def merge_candidate_sets(spec: BriefSpec, base: CandidateSet, extra: CandidateSet) -> CandidateSet:
    """Add a later round's candidates (e.g. people at discovered organisations) to ``base``."""
    candidates = _merge_candidates([*base.candidates, *extra.candidates])
    stats = EnrichmentStats(
        requested=base.enrichment.requested + extra.enrichment.requested,
        succeeded=base.enrichment.succeeded + extra.enrichment.succeeded,
        failed=base.enrichment.failed + extra.enrichment.failed,
        cost_usd=None if None in (base.enrichment.cost_usd, extra.enrichment.cost_usd)
        else round(base.enrichment.cost_usd + extra.enrichment.cost_usd, 6),
    )
    return base.model_copy(update={
        "candidates": candidates, "enrichment": stats, "warnings": base.warnings + extra.warnings,
        "summary": _summarise(spec, candidates, base.organisations),
    })


def canonicalise_organisations(spec: BriefSpec, orgs: list[DiscoveredOrganisation]) -> list[DiscoveredOrganisation]:
    """Rename discoveries that are an organisation the brief names (e.g. "PKP Polskie Linie
    Kolejowe S.A." -> "PKP PLK"), so variants merge and get one people search, not five."""
    named = {o.name.lower(): o for o in [
        *([spec.target_organisation] if spec.target_organisation else []),
        *(o for g in spec.groups for o in g.eligible_organisations),
        *(ex.organisation for ex in spec.excluded_organisations),
    ]}
    out = []
    for o in orgs:
        match = next((n.name for n in named.values()
                      if any(same_organisation(x, o.name) for x in [n.name, *n.aliases])), None)
        out.append(o.model_copy(update={"name": match}) if match else o)
    return out


def _tag_discovered_organisations(
    spec: BriefSpec, candidates: list[Candidate], orgs: list[DiscoveredOrganisation],
) -> None:
    """Match positions against the organisations discovered for the same group.

    People quoted on a case-study page are extracted in the same pass that finds
    the organisations, so their employer could not be matched until now.
    """
    by_group: dict[str, list[Organisation]] = defaultdict(list)
    for o in orgs:
        by_group[o.group_id].append(Organisation(name=o.name, aliases=[]))
    for c in candidates:
        found = by_group.get(c.group_id)
        if not found or spec.group(c.group_id).eligible_organisations:
            continue
        for p in c.positions:
            extra = [n for n in matching_organisations(p.organisation, found) if n not in p.matched_organisations]
            p.matched_organisations += extra


def _cap_pages(
    page_hits: dict[Key, list[SearchHit]], limit: int, warnings: list[str]
) -> dict[Key, list[SearchHit]]:
    """Keep each group's best-ranked pages: rank 1 of every query first, then rank 2, ..."""
    by_group: dict[str, list[tuple[Key, list[SearchHit]]]] = defaultdict(list)
    for key, hits in page_hits.items():
        by_group[key[0]].append((key, hits))
    kept: dict[Key, list[SearchHit]] = {}
    for gid, items in by_group.items():
        items.sort(key=lambda kv: (min(h.rank for h in kv[1]), kv[1][0].provider, kv[0][1]))
        if len(items) > limit:
            warnings.append(f"{gid}: {len(items)} company-discovery pages, extracting the best-ranked {limit}")
        kept.update(dict(items[:limit]))
    return kept


def _merge_candidates(candidates: list[Candidate]) -> list[Candidate]:
    merged: dict[str, Candidate] = {}
    for c in candidates:
        if c.candidate_id not in merged:
            merged[c.candidate_id] = c
            continue
        m = merged[c.candidate_id]
        m.evidence += [e for e in c.evidence if e not in m.evidence]
        m.source_hit_ids = list(dict.fromkeys(m.source_hit_ids + c.source_hit_ids))
        m.query_ids = list(dict.fromkeys(m.query_ids + c.query_ids))
        m.providers = list(dict.fromkeys(m.providers + c.providers))
        m.positions += [p for p in c.positions if p not in m.positions]
    return list(merged.values())


def _merge_organisations(orgs: list[DiscoveredOrganisation]) -> list[DiscoveredOrganisation]:
    merged: dict[tuple[str, str], DiscoveredOrganisation] = {}
    for o in orgs:
        key = (o.group_id, org_key(o.name))
        if key in merged:
            merged[key].evidence += o.evidence
        else:
            merged[key] = o
    return list(merged.values())


def _summarise(
    spec: BriefSpec, candidates: list[Candidate], orgs: list[DiscoveredOrganisation],
    extra: dict[str, list[Organisation]] | None = None,
) -> list[CandidateSummaryRow]:
    rows = []
    for g in spec.groups:
        mine = [c for c in candidates if c.group_id == g.group_id]
        by_method: dict[str, int] = defaultdict(int)
        for c in mine:
            by_method[c.extraction_method] += 1
        discovered = [o.name for o in orgs if o.group_id == g.group_id and o.include]
        discovered += [o.name for o in (extra or {}).get(g.group_id, [])]
        dated = relevant_organisations(spec, g, discovered)
        ok = useful_statuses(g)
        relevant = {o.name for o in g.eligible_organisations}
        relevant |= {o.name for o in orgs if o.group_id == g.group_id and o.include}
        if spec.target_organisation:
            relevant.add(spec.target_organisation.name)
        rows.append(CandidateSummaryRow(
            group_id=g.group_id,
            candidates=len(mine),
            by_method=dict(by_method),
            with_matched_position=sum(
                1 for c in mine if any(set(p.matched_organisations) & relevant for p in c.positions)
            ),
            useful=sum(1 for c in mine if departure_status(c, g, dated) in ok),
            organisations=sum(1 for o in orgs if o.group_id == g.group_id),
        ))
    return rows

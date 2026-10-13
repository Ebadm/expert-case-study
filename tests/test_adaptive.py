"""Early stopping: extraction chunks, the Exa people top-up and stage-5 batches."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone

import pytest

from sourcing_agent.adaptive import RoundContext, deepen, first_round, top_up
from sourcing_agent.brief.parser import build_spec
from sourcing_agent.config import ExtractionSettings, LLMPricing, SearchSettings, Settings
from sourcing_agent.extraction.candidate_extractor import extract_candidates
from sourcing_agent.extraction.enrichment import PageContent
from sourcing_agent.models import (
    CandidateSet,
    DiscoveredOrganisation,
    GeneratorInfo,
    GroupQueryPlan,
    OrganisationSet,
    PageExtractionDraft,
    PageOrganisationDraft,
    ParserInfo,
    QueryPlan,
    QueryRun,
    SearchConfig,
    SearchHit,
    SearchQuery,
    SearchResults,
)
from sourcing_agent.people_search.organisations import ORG_BATCH, run_organisation_people

from .conftest import FakeLLM
from .test_extraction import FakeContents, hit

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
GEN = GeneratorInfo(model="fake", effort="medium", generated_at=NOW)


def person(name: str, company: str = "Tungsten Automation", to: str | None = "2026-01-01") -> dict:
    """An Exa person entity: left ``company`` in Jan 2026 (inside the former-employee window)."""
    return {"type": "person", "properties": {"name": name, "workHistory": [
        {"title": "Manager", "company": {"name": company}, "dates": {"from": "2020-01-01", "to": to}},
    ]}}


@pytest.fixture
def spec(tungsten_draft, tungsten_text):
    return build_spec(tungsten_draft, text=tungsten_text, brief_id="tungsten_automation", source_path="b.md",
                      run_date=date(2026, 10, 10), parser=ParserInfo(model="f", effort="medium", parsed_at=NOW))


def settings(target=40, batch=50, orgs_wanted=40) -> Settings:
    return Settings(
        llm_model="fake", llm_effort="medium", anthropic_api_key=None, llm_pricing=LLMPricing(0.1, 0.5, "t"),
        org_people_queries_per_group=orgs_wanted, target_candidates_per_group=target,
        search=SearchSettings(exa_api_key=None),
        extraction=ExtractionSettings(max_pages_per_group=100, llm_workers=2, batch_size=batch),
    )


def results(spec, hits) -> SearchResults:
    return SearchResults(brief_id=spec.brief_id, source_sha256=spec.source.sha256, executed_at=NOW,
                         config=SearchConfig(serper_max_pages=1, exa_num_results=10, exa_text_max_chars=100),
                         summary=[], runs=[], hits=hits)


def empty_set(spec, orgs=()) -> CandidateSet:
    return CandidateSet(brief_id=spec.brief_id, source_sha256=spec.source.sha256, extracted_at=NOW,
                        extractor=GEN, summary=[], enrichment={}, candidates=[], organisations=list(orgs))


# --- extraction --------------------------------------------------------------- #

def test_profiles_stop_once_the_group_has_its_target(spec):
    urls = [f"https://www.linkedin.com/in/p{i}" for i in range(5)]
    contents = FakeContents({u: PageContent(url=u, title=None, text="x", published_date=None,
                                            entities=[person(f"P {i}")]) for i, u in enumerate(urls)})
    hits = [hit(u, rank=i + 1) for i, u in enumerate(urls)]

    cs = extract_candidates(spec, results(spec, hits), llm=FakeLLM(), settings=settings(target=2, batch=1),
                            extractor=GEN, contents=contents)

    assert contents.asked == urls[:2]  # the other three were never enriched
    assert len(cs.candidates) == 2
    assert next(r for r in cs.summary if r.group_id == "former_employees").useful == 2
    assert any("after 2 of 5 profiles" in w for w in cs.warnings)


def test_base_candidates_count_toward_the_target(spec):
    urls = [f"https://www.linkedin.com/in/p{i}" for i in range(3)]
    contents = FakeContents({u: PageContent(url=u, title=None, text="x", published_date=None,
                                            entities=[person(f"P {i}")]) for i, u in enumerate(urls)})
    first = extract_candidates(spec, results(spec, [hit(urls[0])]), llm=FakeLLM(), settings=settings(),
                               extractor=GEN, contents=contents)
    contents.asked.clear()

    extract_candidates(spec, results(spec, [hit(u, rank=i + 1) for i, u in enumerate(urls[1:])]),
                       llm=FakeLLM(), settings=settings(target=2, batch=1), extractor=GEN, contents=contents,
                       base_candidates=first.candidates)

    assert contents.asked == [urls[1]]


def test_discovery_pages_stop_once_enough_organisations_are_found(spec):
    urls = [f"https://example.com/case-{i}" for i in range(3)]
    contents = FakeContents({u: PageContent(url=u, title="c", text="TUI Group runs Kofax.", published_date=None)
                             for u in urls})
    draft = PageExtractionDraft(people=[], organisations=[PageOrganisationDraft(
        name="TUI Group", relationship="customer", quote="TUI Group runs Kofax.")])
    hits = [hit(u, group="customers", qtype="company", qid="customers.company.serper.01", rank=i + 1)
            for i, u in enumerate(urls)]

    cs = extract_candidates(spec, results(spec, hits), llm=FakeLLM(draft),
                            settings=settings(batch=1, orgs_wanted=1), extractor=GEN, contents=contents)

    assert contents.asked == urls[:1]
    assert [o.name for o in cs.organisations] == ["TUI Group"]
    assert any("after 1 of 3 pages" in w for w in cs.warnings)


# --- Exa people top-up ---------------------------------------------------------- #

class ProfileClient:
    """Returns one LinkedIn profile per query; the profile carries an Exa person entity."""

    def __init__(self, provider: str, company=lambda q: "Tungsten Automation", to: str | None = "2026-01-01"):
        self.provider, self.company, self.to = provider, company, to
        self.cost_source = "test"
        self.seen: list[str] = []

    def search(self, q: SearchQuery):
        self.seen.append(q.query_id)
        url = f"https://linkedin.com/in/{q.query_id.replace('.', '-')}"
        h = SearchHit(hit_id=f"{q.query_id}#1", query_id=q.query_id, group_id=q.group_id, provider=q.provider,
                      query_type="people", organisation=q.organisation, rank=1, page=None,
                      title=f"{q.query_id} | LinkedIn", url=url,
                      canonical_url=url, snippet=None, text="x",
                      entities=[person(q.query_id, self.company(q), self.to)], retrieved_at=NOW)
        return [h], QueryRun(query_id=q.query_id, group_id=q.group_id, provider=q.provider, query_type="people",
                             query=q.query, requests=1, results=1, stop_reason="single_request", duration_s=0.0,
                             cost_usd=0.001)


def people_plan(spec, group="former_employees") -> QueryPlan:
    def q(provider, n):
        return SearchQuery(query_id=f"{group}.people.{provider}.{n:02d}", group_id=group, provider=provider,
                           query_type="people", query="q", language="en", intent="i")
    return QueryPlan(brief_id=spec.brief_id, source_sha256=spec.source.sha256, spec_parsed_at=NOW, generator=GEN,
                     queries_per_provider=5, groups=[GroupQueryPlan(
                         group_id=group, mode="people", queries=[q("serper", 1), q("serper", 2), q("exa", 1)])])


def run_with_top_up(spec, target):
    plan = people_plan(spec)
    clients = {"serper": ProfileClient("serper"), "exa": ProfileClient("exa")}
    ctx = RoundContext(spec=spec, settings=settings(target=target), llm=FakeLLM(), clients=clients)
    from sourcing_agent.search.runner import run_searches
    search = run_searches(first_round(plan, ctx.settings), ctx.settings.search, clients=clients)
    cs = extract_candidates(spec, search, llm=ctx.llm, settings=ctx.settings, extractor=GEN)
    search, cs = top_up(ctx, plan, search, cs)
    return clients, search, cs


def test_exa_top_up_is_skipped_when_google_reached_the_target(spec):
    clients, search, cs = run_with_top_up(spec, target=2)
    assert clients["serper"].seen == ["former_employees.people.serper.01", "former_employees.people.serper.02"]
    assert clients["exa"].seen == []
    assert any("1 Exa people queries not run" in w for w in cs.warnings)


def test_exa_top_up_runs_for_a_group_short_of_the_target(spec):
    clients, search, cs = run_with_top_up(spec, target=5)
    assert clients["exa"].seen == ["former_employees.people.exa.01"]
    assert [r.query_id for r in search.runs][-1] == "former_employees.people.exa.01"
    assert len(cs.candidates) == 3


def test_without_a_target_the_first_round_is_the_whole_plan(spec):
    plan = people_plan(spec)
    assert first_round(plan, settings(target=0)) == plan
    assert [q.provider for q in first_round(plan, settings()).queries] == ["serper", "serper"]


# --- later Google pages ------------------------------------------------------------ #

class PagingClient:
    """Google-like: 10 profiles per page, 3 pages per X-ray; honours first_page / max_pages."""

    provider, cost_source, cap = "serper", "test", 3

    def __init__(self, company=lambda q: "Tungsten Automation", to: str | None = "2026-01-01"):
        self.company, self.to = company, to
        self.seen: list[str] = []

    def search(self, q: SearchQuery):
        self.seen.append(q.query_id)
        base = q.query_id.split("+")[0].replace(".", "-")
        last = self.cap if q.max_pages is None else min(self.cap, q.first_page + q.max_pages - 1)
        hits = []
        for page in range(q.first_page, last + 1):
            for i in range(10):
                url = f"https://linkedin.com/in/{base}-{page}-{i}"
                rank = (q.first_page - 1) * 10 + len(hits) + 1
                hits.append(SearchHit(
                    hit_id=f"{q.query_id}#{rank}", query_id=q.query_id, group_id=q.group_id, provider="serper",
                    query_type="people", organisation=q.organisation, rank=rank, page=page, title=url, url=url,
                    canonical_url=url, snippet=None, text="x",
                    entities=[person(f"P {base} {page} {i}", self.company(q), self.to)], retrieved_at=NOW))
        return hits, QueryRun(query_id=q.query_id, group_id=q.group_id, provider="serper", query_type="people",
                              query=q.query, requests=last - q.first_page + 1, results=len(hits),
                              stop_reason="max_pages", duration_s=0.0, cost_usd=0.001)


def run_with_deepen(spec, target):
    plan = people_plan(spec)
    clients = {"serper": PagingClient()}
    ctx = RoundContext(spec=spec, settings=settings(target=target), llm=FakeLLM(), clients=clients)
    from sourcing_agent.search.runner import run_searches
    search = run_searches(first_round(plan, ctx.settings), ctx.settings.search, clients=clients)
    cs = extract_candidates(spec, search, llm=ctx.llm, settings=ctx.settings, extractor=GEN)
    search, cs = deepen(ctx, plan, search, cs)
    return clients["serper"], search, cs


def test_xrays_fetch_page_one_first(spec):
    plan = first_round(people_plan(spec), settings())
    assert [(q.provider, q.max_pages) for q in plan.queries] == [("serper", 1), ("serper", 1)]
    assert first_round(people_plan(spec), settings(target=0)) == people_plan(spec)  # no target: full depth


def test_later_pages_are_fetched_only_for_a_group_still_short(spec):
    serper, search, cs = run_with_deepen(spec, target=60)  # 20 from page 1 is not enough
    q1, q2 = "former_employees.people.serper.01", "former_employees.people.serper.02"
    assert serper.seen[2:] == [f"{q1}+p2", f"{q2}+p2", f"{q1}+p3", f"{q2}+p3"]  # one page at a time
    later = [h for h in search.hits if "+p" in h.query_id]
    assert min(h.rank for h in later) == 11 and {h.page for h in later} == {2, 3}
    assert len(cs.candidates) == 60

    serper, search, cs = run_with_deepen(spec, target=30)  # page 2 is enough: no page 3
    assert serper.seen[2:] == [f"{q1}+p2", f"{q2}+p2"]

    serper, search, cs = run_with_deepen(spec, target=15)  # page 1 already had enough
    assert serper.seen == ["former_employees.people.serper.01", "former_employees.people.serper.02"]


def test_later_pages_skip_profiles_already_read(spec, monkeypatch):
    import sourcing_agent.adaptive as adaptive
    plan = people_plan(spec)
    client = PagingClient()
    ctx = RoundContext(spec=spec, settings=settings(target=60), llm=FakeLLM(), clients={"serper": client})
    search = adaptive.run_searches(first_round(plan, ctx.settings), ctx.settings.search, clients=ctx.clients)
    cs = extract_candidates(spec, search, llm=ctx.llm, settings=ctx.settings, extractor=GEN)
    repeat = search.hits[0]  # Google repeating a page-1 profile on page 2
    original = client.search
    client.search = lambda q: (
        ([repeat.model_copy(update={"query_id": q.query_id, "hit_id": f"{q.query_id}#99"}), *original(q)[0]],
         original(q)[1]) if q.first_page > 1 else original(q))
    read: list[str] = []
    real = adaptive.extract_candidates

    def spy(spec_, search_, **kw):
        read.extend(h.canonical_url for h in search_.hits)
        return real(spec_, search_, **kw)
    monkeypatch.setattr(adaptive, "extract_candidates", spy)
    search, _ = deepen(ctx, plan, search, cs)
    assert repeat.canonical_url not in read and len(read) == 40  # pages 2-3 of both X-rays, nothing re-read
    assert sum(h.canonical_url == repeat.canonical_url for h in search.hits) == 5  # provenance kept


# --- stage 5 batches ------------------------------------------------------------- #

def org_set(spec, n) -> OrganisationSet:
    return OrganisationSet(brief_id=spec.brief_id, source_sha256=spec.source.sha256, created_at=NOW, organisations=[
        DiscoveredOrganisation(group_id="customers", name=f"Customer {i:02d}", relationship="customer", evidence=[])
        for i in range(n)
    ])


@pytest.mark.parametrize("target, expected_queries", [(3, ORG_BATCH), (0, ORG_BATCH + 2)])
def test_stage5_stops_after_the_batch_that_reaches_the_target(spec, target, expected_queries):
    orgs = org_set(spec, ORG_BATCH + 2)
    serper = ProfileClient("serper", company=lambda q: q.organisation, to=None)  # current staff at the customer
    ctx = RoundContext(spec=spec, settings=settings(target=target), llm=FakeLLM(), clients={"serper": serper})

    plan, search, cs = run_organisation_people(ctx, None, orgs, empty_set(spec, orgs.organisations))

    google = [q for q in plan.queries if q.provider == "serper"]
    assert len(google) == ORG_BATCH + 2  # the plan lists every organisation
    assert len(serper.seen) == expected_queries == sum(r.provider == "serper" for r in search.runs)
    assert len(cs.for_group("customers")) == expected_queries
    if target:  # neither the last Google batch nor Exa ran
        assert not any(r.provider == "exa" for r in search.runs)
        assert any(f"{len(plan.queries) - ORG_BATCH} people queries not run" in w for w in cs.warnings)


def test_stage5_fetches_later_pages_after_every_organisation_was_searched(spec):
    orgs = org_set(spec, 2)
    serper = PagingClient(company=lambda q: q.organisation, to=None)  # current staff at the customer
    ctx = RoundContext(spec=spec, settings=settings(target=50), llm=FakeLLM(), clients={"serper": serper})
    plan, search, cs = run_organisation_people(ctx, None, orgs, empty_set(spec, orgs.organisations))
    assert [q.partition("+p")[2] for q in serper.seen] == ["", "", "2", "2", "3", "3"]
    assert len(cs.for_group("customers")) == 60


def test_stage5_without_organisations_runs_nothing(spec):
    serper = ProfileClient("serper")
    ctx = RoundContext(spec=spec, settings=dataclasses.replace(settings()), llm=FakeLLM(), clients={"serper": serper})
    plan, search, cs = run_organisation_people(ctx, None, org_set(spec, 0), empty_set(spec))
    assert plan.queries == [] and search is None and serper.seen == []


# --- qualification gate -------------------------------------------------------------

def test_profiles_stop_on_the_gate_not_on_dates(spec):
    """Everyone's dates fit, but only "leads" qualify: reading continues until 2 leads."""
    urls = [f"https://www.linkedin.com/in/p{i}" for i in range(6)]
    names = ["Ann Clerk", "Bob Clerk", "Cy Lead", "Di Clerk", "Ed Lead", "Fay Lead"]
    contents = FakeContents({u: PageContent(url=u, title=None, text="x", published_date=None,
                                            entities=[person(n)]) for u, n in zip(urls, names)})
    hits = [hit(u, rank=i + 1) for i, u in enumerate(urls)]

    def gate(group, candidates):
        return sum(1 for c in candidates if "Lead" in c.name)

    cs = extract_candidates(spec, results(spec, hits), llm=FakeLLM(), settings=settings(target=2, batch=1),
                            extractor=GEN, contents=contents, gate=gate)
    assert contents.asked == urls[:5]  # stopped after the second lead, not after two date-fit people
    assert any("2 qualified candidates" in w for w in cs.warnings)


def ranking_llm(verdict_for):
    """A fake LLM answering RankingDraft calls: verdict_for(name) -> verdict."""
    from sourcing_agent.models import CandidateAssessmentDraft, RankingDraft

    def answer(user: str) -> RankingDraft:
        out = []
        for block in user.split('<candidate ref="')[1:]:
            ref, name = block.split('"')[0], block.split("name: ")[1].split("\n")[0]
            out.append(CandidateAssessmentDraft(
                ref=ref, verdict=verdict_for(name), unmet_requirement=None, role_fit=3, relevance=3,
                seniority=3, evidence_strength=3, reason=f"{name}.", evidence_ref=f"{ref}.E1"))
        return RankingDraft(assessments=out)
    return FakeLLM(by_model={RankingDraft: answer})


def test_assessor_counts_verified_meets_and_caches_verdicts(spec):
    from sourcing_agent.ranking.assessor import Assessor
    from sourcing_agent.resolution.identity import resolve

    urls = ["https://linkedin.com/in/a", "https://linkedin.com/in/b", "https://linkedin.com/in/c"]
    hits = [hit(u, provider="exa", rank=i + 1, text="x", entities=[person(n, to=to)])
            for i, (u, n, to) in enumerate(zip(urls, ["Ann Lead", "Bob Clerk", "Cy Current"],
                                                ["2026-01-01", "2026-01-01", None]))]
    cs = extract_candidates(spec, results(spec, hits), llm=FakeLLM(), settings=settings(), extractor=GEN)
    llm = ranking_llm(lambda n: "meets" if "Lead" in n else "does_not_meet")
    assessor = Assessor(spec, llm, settings())
    group = spec.group("former_employees")
    assert assessor.qualified(group, cs.candidates) == 1  # Cy is still current: never sent to the LLM
    assert assessor.asked == 2 and len(llm.calls) == 1
    assert assessor.qualified(group, cs.candidates) == 1
    assert len(llm.calls) == 1 and assessor.reused == 2  # answered from the cache
    assert resolve(spec, cs).stats.identities == 3


def test_reranking_unchanged_candidates_reuses_saved_verdicts(spec):
    from sourcing_agent.ranking.assessor import Assessor, cache_from
    from sourcing_agent.ranking.ranker import rank
    from sourcing_agent.resolution.identity import resolve
    from sourcing_agent.verification.eligibility import verify_all

    hits = [hit(f"https://linkedin.com/in/p{i}", provider="exa", rank=i + 1, text="x",
                entities=[person(f"Person Number{i}")]) for i in range(3)]
    cs = extract_candidates(spec, results(spec, hits), llm=FakeLLM(), settings=settings(), extractor=GEN)
    r = resolve(spec, cs)
    v = verify_all(spec, r)
    first = rank(spec, r, v, llm=ranking_llm(lambda n: "meets"), settings=settings())
    llm = ranking_llm(lambda n: "does_not_meet")  # would change the answer if asked
    again = rank(spec, r, v, llm=llm, settings=settings(),
                 assessor=Assessor(spec, llm, settings(), cache=cache_from(first)))
    assert llm.calls == []
    assert [e.name for g in again.groups for e in g.experts] == [e.name for g in first.groups for e in g.experts]


def test_stage5_keeps_searching_while_nobody_qualifies(spec):
    from sourcing_agent.ranking.assessor import Assessor

    orgs = org_set(spec, ORG_BATCH + 2)
    serper = ProfileClient("serper", company=lambda q: q.organisation, to=None)  # current staff: dates fit
    s = settings(target=3)
    llm = ranking_llm(lambda n: "unclear")  # the LLM is never sure they meet the group
    ctx = RoundContext(spec=spec, settings=s, llm=llm, clients={"serper": serper},
                       assessor=Assessor(spec, llm, s))
    plan, search, cs = run_organisation_people(ctx, None, orgs, empty_set(spec, orgs.organisations))
    assert len(serper.seen) == ORG_BATCH + 2  # date-fit counting would have stopped after 10


def test_seniority_words_give_each_organisation_a_senior_query_first(spec):
    from sourcing_agent.models import Organisation, SearchTermsDraft
    from sourcing_agent.search.people_plan import build_organisation_people_queries

    group = spec.group("customers")
    terms = SearchTermsDraft(role_keywords=["operations", "fleet"], topic_keywords=[],
                             title_keywords=["head", "director", "kierownik"])
    orgs = [Organisation(name="CTL Logistics", aliases=[]), Organisation(name="Captrain Polska", aliases=[])]
    qs = [q for q in build_organisation_people_queries(spec, group, orgs, terms, max_xray=10, max_exa=0)
          if q.provider == "serper"]
    assert len(qs) == 4
    assert all('"kierownik"' in q.query for q in qs[:2]) and not any('"kierownik"' in q.query for q in qs[2:])
    assert [q.organisation for q in qs] == ["CTL Logistics", "Captrain Polska"] * 2


def test_named_organisation_groups_get_a_role_and_seniority_slice_first(spec):
    from sourcing_agent.models import SearchTermsDraft
    from sourcing_agent.search.people_plan import build_people_queries

    group = spec.group("competitors")
    terms = SearchTermsDraft(role_keywords=["product", "partnerships"], topic_keywords=[],
                             title_keywords=["vp", "head", "director"])
    qs, _ = build_people_queries(spec, group, group.eligible_organisations, terms, 1, max_xray=20)
    first = [q for q in qs if q.provider == "serper"][0]
    assert '"director"' in first.query and '"product"' in first.query and "role and seniority" in first.intent
    plain, _ = build_people_queries(spec, group, group.eligible_organisations,
                                    terms.model_copy(update={"title_keywords": []}), 1, max_xray=20)
    assert not any("seniority" in q.intent for q in plain)

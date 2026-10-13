"""Entity resolution, deterministic verification and ranking plus profile lookup (stages 6-9)."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timezone

import pytest

from sourcing_agent import pipeline
from sourcing_agent.brief.parser import build_spec
from sourcing_agent.config import ExtractionSettings, LLMPricing, SearchSettings, Settings
from sourcing_agent.models import (
    Candidate,
    CandidateAssessmentDraft,
    CandidateSet,
    DiscoveredOrganisation,
    Evidence,
    GeneratorInfo,
    Organisation,
    OrganisationExclusion,
    ParserInfo,
    Position,
    QueryRun,
    RankingDraft,
    SamePersonDraft,
    SearchHit,
)
from sourcing_agent.ranking.assessor import Assessor
from sourcing_agent.ranking.ranker import rank
from sourcing_agent.resolution.identity import name_relation, name_tokens, resolve
from sourcing_agent.verification.eligibility import role_pattern, verify_all

from .conftest import FakeLLM

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
RUN_DATE = date(2026, 10, 10)
GEN = GeneratorInfo(model="fake", effort="medium", generated_at=NOW)


@pytest.fixture
def spec(tungsten_draft, tungsten_text):
    s = build_spec(tungsten_draft, text=tungsten_text, brief_id="tungsten_automation", source_path="b.md",
                   run_date=RUN_DATE, parser=ParserInfo(model="f", effort="medium", parsed_at=NOW))
    # Make the customers group relative to the target, as the real parse is.
    groups = [g.model_copy(update={"eligible_organisation_types": ["Companies running Tungsten Automation products"]})
              if g.group_id == "customers" else g for g in s.groups]
    return s.model_copy(update={"groups": groups})


def pos(org, title="Product Manager", start=(2019, 1), end=(2026, 1), current=False, matched=None) -> Position:
    return Position(
        title=title, organisation=org, start=date(*start, 1) if start else None,
        end=None if current or end is None else date(*end, 1), is_current=True if current else (None if end is None else False),
        date_precision="month" if start or end else "unknown",
        matched_organisations=matched if matched is not None else [], source="exa_entities",
    )


def ev(url, quote="quote", claim="claim", etype="provider_structured", stype="profile") -> Evidence:
    return Evidence(source_url=url, source_type=stype, provider="exa", retrieved_at=NOW, claim=claim,
                    supporting_text=quote, evidence_type=etype, supports=["employment"])


def cand(cid, name, group="former_employees", url=None, positions=(), method="exa_entities", headline=None,
         evidence=None, target_mentioned=None) -> Candidate:
    canon = f"https://linkedin.com/in/{url}" if url else None
    return Candidate(
        candidate_id=cid, group_id=group, name=name, profile_url=canon, canonical_url=canon, headline=headline,
        positions=list(positions), evidence=evidence if evidence is not None else ([ev(canon)] if canon else []),
        extraction_method=method, target_mentioned=target_mentioned,
    )


def cset(spec, candidates, orgs=()) -> CandidateSet:
    return CandidateSet(brief_id=spec.brief_id, source_sha256=spec.source.sha256, extracted_at=NOW, extractor=GEN,
                        summary=[], enrichment={}, candidates=list(candidates), organisations=list(orgs))


TA = ["Tungsten Automation"]


# --- entity resolution ------------------------------------------------------- #

@pytest.mark.parametrize("a, b, expected", [
    ("Dr. Jane Doe, MBA", "Jane Doe", "same"),
    ("Paweł Skowroński", "Pawel Skowronski", "same"),
    ("John A. Smith", "John Smith", "same"),
    ("Matthew T.", "Matthew Thompson", "near"),
    ("Jon Smyth", "Jon Smith", "near"),
    ("Jane Doe", "John Doe", None),
    ("Madonna", "Madonna", None),  # one-word names never match
])
def test_name_relation(a, b, expected):
    assert name_relation(a, b) == expected


def test_name_tokens_keep_surnames_that_look_like_credentials():
    assert name_tokens("Lin Ma") == ["lin", "ma"]
    assert name_tokens("Jane Doe (she/her) | Product Lead") == ["jane", "doe"]


def test_same_profile_url_merges_across_groups_and_records_the_overlap(spec):
    a = cand("former_employees:jd", "Jane Doe", url="jd", positions=[pos("Kofax", matched=TA)])
    b = cand("customers:jd", "Jane Doe", group="customers", url="jd", positions=[pos("Serco", current=True)])
    r = resolve(spec, cset(spec, [a, b]))
    assert r.stats.identities == 1 and r.stats.records_out == 2 and r.stats.cross_group_identities == 1
    assert {c.group_id: c.also_in_groups for c in r.candidates} == {
        "former_employees": ["customers"], "customers": ["former_employees"]}
    assert r.identities[0].identity_id == "profile:jd"


def test_page_person_merges_into_profile_on_name_and_employer(spec):
    profile = cand("customers:jd", "Jane Doe", group="customers", url="jd",
                   positions=[pos("TUI Group plc", current=True)])
    page = cand("customers:page:jane_doe-tui", "Jane Doe", group="customers", method="llm_page",
                positions=[pos("TUI Group", title="Head of AP", start=None, end=None)],
                evidence=[ev("https://tui.example/case", quote="said Jane Doe", etype="quote", stype="web_page")])
    r = resolve(spec, cset(spec, [profile, page]))
    assert r.stats.records_out == 1 and r.stats.within_group_duplicates == 1
    merged = r.candidates[0]
    assert merged.profile_url == "https://linkedin.com/in/jd"  # the profile is the base record
    assert {e.source_url for e in merged.evidence} == {"https://linkedin.com/in/jd", "https://tui.example/case"}
    assert r.identities[0].links[0].rule == "name_and_organisation"


def test_same_name_without_a_shared_employer_is_not_merged(spec):
    a = cand("customers:page:brian-a", "Brian Harris", group="customers", method="llm_page",
             positions=[pos("Loma Linda University", start=None, end=None)])
    b = cand("customers:page:brian-b", "Brian Harris", group="customers", method="llm_page",
             positions=[pos("Hertz", start=None, end=None)])
    assert resolve(spec, cset(spec, [a, b])).stats.identities == 2


def test_two_profiles_with_the_same_name_and_employer_go_to_the_llm(spec):
    a = cand("terminal:gc1", "Gilbert Conter", group="customers", url="gc1",
             positions=[pos("CFL Multimodal S.A.", start=(2015, 1), current=True)])
    b = cand("terminal:gc2", "Gilbert Conter", group="customers", url="gc2",
             positions=[pos("CFL Multimodal", start=(2016, 1), current=True)])
    assert resolve(spec, cset(spec, [a, b])).stats.identities == 2  # no LLM: kept apart
    llm = FakeLLM(SamePersonDraft(same_person=True, reason="same employer and title since 2015"))
    r = resolve(spec, cset(spec, [a, b]), llm=llm)
    assert r.stats.identities == 1 and r.stats.llm_merges == 1 and len(llm.calls) == 1
    assert r.identities[0].links[0].rule == "llm_same_person"
    llm_no = FakeLLM(SamePersonDraft(same_person=False, reason="different careers"))
    assert resolve(spec, cset(spec, [a, b]), llm=llm_no).stats.identities == 2


# --- verification -------------------------------------------------------------- #

def decisions(spec, candidates, orgs=()):
    r = resolve(spec, cset(spec, candidates, orgs))
    return {v.candidate_id: v for v in verify_all(spec, r).decisions}


def test_former_employee_window_and_current_staff(spec):
    ok = cand("former_employees:ok", "Ann Left", url="ok", positions=[pos("Kofax Inc.", end=(2025, 1), matched=TA)])
    recent = cand("former_employees:recent", "Bob Recent", url="recent",
                  positions=[pos("Tungsten Automation", end=(2026, 8), matched=TA)])
    current = cand("former_employees:cur", "Cat Current", url="cur",
                   positions=[pos("Tungsten Automation", current=True, matched=TA)])
    undated = cand("former_employees:und", "Dan Undated", url="und",
                   positions=[pos("Kofax", start=None, end=None, matched=TA)])
    d = decisions(spec, [ok, recent, current, undated])
    assert d["former_employees:ok"].decision == "eligible"
    assert d["former_employees:recent"].decision == "ineligible"
    assert d["former_employees:cur"].decision == "ineligible"
    assert {r.rule for r in d["former_employees:cur"].reasons()} == {"employment_status", "exclusion"}
    assert d["former_employees:und"].decision == "needs_review"


@pytest.mark.parametrize("employer", ["Kofax India Pvt. Ltd.", "Tungsten Network, a Kofax company"])
def test_current_staff_of_alias_entities_are_excluded_from_other_groups(spec, employer):
    c = cand("competitors:x", "Eve Alias", group="competitors", url="x",
             positions=[pos("ABBYY", title="VP Product", end=(2025, 6), matched=["ABBYY"]),
                        pos(employer, title="Director", start=(2025, 7), current=True)])
    v = decisions(spec, [c])["competitors:x"]
    assert v.decision == "ineligible" and v.reasons()[0].rule == "exclusion"


def test_headline_at_excluded_company_overrides_a_stale_record(spec):
    c = cand("competitors:h", "Hal Headline", group="competitors", url="h", headline="Product Lead at Kofax",
             positions=[pos("ABBYY", title="Product Director", current=True, matched=["ABBYY"])])
    assert decisions(spec, [c])["competitors:h"].decision == "ineligible"
    c2 = c.model_copy(update={"headline": "Product Director at ABBYY | ex-Kofax", "candidate_id": "competitors:h2",
                              "canonical_url": "https://linkedin.com/in/h2"})
    assert decisions(spec, [c2])["competitors:h2"].decision == "eligible"


@pytest.mark.parametrize("phrase, title, hit", [
    ("C-level", "Chief Product Officer", True),
    ("C-level", "CTO, IBM India", True),
    ("C-level", "Vice President, Product", False),
    ("C-level", "Director of Customer Success", False),
    ("founder", "Co-Founder & VP Sales", True),
    ("intern", "Product Intern", True),
])
def test_excluded_role_patterns(phrase, title, hit):
    assert bool(role_pattern(phrase).search(title)) is hit


def test_customers_need_a_source_naming_the_target(spec):
    org = DiscoveredOrganisation(group_id="customers", name="Serco", relationship="customer", evidence=[])
    named = cand("customers:a", "Amy Uses", group="customers", url="a", target_mentioned=True,
                 positions=[pos("Serco", current=True, matched=["Serco"])])
    silent = cand("customers:b", "Ben Silent", group="customers", url="b", target_mentioned=False,
                  positions=[pos("Serco", current=True, matched=["Serco"])])
    d = decisions(spec, [named, silent], [org])
    assert d["customers:a"].decision == "eligible"
    assert d["customers:b"].decision == "ineligible" and d["customers:b"].reasons()[0].rule == "target_evidence"


@pytest.mark.parametrize("quote, decision", [
    ("Led the Kofax Capture rollout for accounts payable", "eligible"),
    ("Built Kofaxian-style reports in Excel", "ineligible"),  # a word containing the name is not a mention
])
def test_target_evidence_needs_the_target_as_a_whole_word(spec, quote, decision):
    org = DiscoveredOrganisation(group_id="customers", name="Serco", relationship="customer", evidence=[])
    c = cand("customers:q", "Quinn Quote", group="customers", url="q", target_mentioned=None,
             evidence=[ev("https://linkedin.com/in/q", quote=quote)],
             positions=[pos("Serco", current=True, matched=["Serco"])])
    assert decisions(spec, [c], [org])["customers:q"].decision == decision


def test_current_only_exclusion_keeps_former_staff_eligible(spec):
    excl = OrganisationExclusion(organisation=Organisation(name="ABBYY", aliases=[]), scope="current_employees",
                                 applies_to_group_ids=[], source_quote="q")
    s = spec.model_copy(update={"excluded_organisations": [*spec.excluded_organisations, excl]})
    former = cand("competitors:f", "Fay Former", group="competitors", url="f",
                  positions=[pos("ABBYY", title="Director Product", end=(2025, 3), matched=["ABBYY"])])
    current = cand("competitors:c", "Cal Current", group="competitors", url="c",
                   positions=[pos("ABBYY", title="Director Product", current=True, matched=["ABBYY"])])
    d = decisions(s, [former, current])
    assert d["competitors:f"].decision == "eligible" and d["competitors:c"].decision == "ineligible"


# --- ranking -------------------------------------------------------------------- #

def assess(scores: dict[str, tuple[bool, int]]):
    """A fake LLM answer: per candidate name, (meets definition, role_fit); cites the first evidence."""
    def answer(user: str) -> RankingDraft:
        out = []
        for block in user.split('<candidate ref="')[1:]:
            ref = block.split('"')[0]
            name = block.split("name: ")[1].split("\n")[0]
            meets, fit = scores[name]
            out.append(CandidateAssessmentDraft(
                ref=ref, verdict=meets if isinstance(meets, str) else ("meets" if meets else "does_not_meet"),
                unmet_requirement=None if meets is True or meets == "meets" else "role",
                role_fit=fit, relevance=3, seniority=3, evidence_strength=3, reason=f"{name} fits.",
                evidence_ref=f"{ref}.E1"))
        return RankingDraft(assessments=out)
    return answer


def settings(max_per_group=20) -> Settings:
    return Settings(llm_model="fake", llm_effort="medium", anthropic_api_key=None,
                    llm_pricing=LLMPricing(0.1, 0.5, "t"), max_experts_per_group=max_per_group,
                    search=SearchSettings(), extraction=ExtractionSettings(llm_workers=2))


def test_ranking_returns_only_eligible_in_score_order_with_cited_evidence(spec):
    people = [
        cand("former_employees:a", "Ann Strong", url="a", positions=[pos("Kofax", end=(2026, 1), matched=TA)],
             evidence=[ev("https://linkedin.com/in/a", quote="EVP Product, Kofax 2019-2026")]),
        cand("former_employees:b", "Bea Weaker", url="b", positions=[pos("Kofax", end=(2024, 1), matched=TA)]),
        cand("former_employees:c", "Cy Rejected", url="c", positions=[pos("Kofax", end=(2025, 1), matched=TA)]),
        cand("former_employees:d", "Di Current", url="d", positions=[pos("Kofax", current=True, matched=TA)]),
    ]
    r = resolve(spec, cset(spec, people))
    v = verify_all(spec, r)
    llm = FakeLLM(by_model={RankingDraft: assess({"Ann Strong": (True, 5), "Bea Weaker": (True, 2),
                                                   "Cy Rejected": (False, 5)})})
    final = rank(spec, r, v, llm=llm, settings=settings())
    group = next(g for g in final.groups if g.group_id == "former_employees")
    assert [e.name for e in group.experts] == ["Ann Strong", "Bea Weaker"]  # Di (current) never reaches ranking
    assert group.eligible == 3 and group.rejected_by_ranking == 1
    top = group.experts[0]
    assert top.evidence.quote == "EVP Product, Kofax 2019-2026" and top.evidence.url == "https://linkedin.com/in/a"
    assert top.company == "Kofax" and top.status == "left Jan 2026" and top.rank == 1
    w = final.weights
    expected = 100 * (w["role_fit"] * 1.0 + w["relevance"] * 0.6 + w["seniority"] * 0.6
                      + w["evidence_strength"] * 0.6 + w["recency"] * top.score.recency
                      + w["corroboration"] * top.score.corroboration)
    assert top.score.total == pytest.approx(expected, abs=0.1)
    assert all("Di Current" not in call["user"] for call in llm.calls)


def test_a_failed_ranking_call_leaves_candidates_unranked_with_a_warning(spec, caplog):
    def fail(user):
        raise RuntimeError("API timeout")
    person = cand("former_employees:a", "Ann Strong", url="a", positions=[pos("Kofax", end=(2026, 1), matched=TA)])
    r = resolve(spec, cset(spec, [person]))
    final = rank(spec, r, verify_all(spec, r), llm=FakeLLM(by_model={RankingDraft: fail}), settings=settings())
    group = next(g for g in final.groups if g.group_id == "former_employees")
    assert group.experts == [] and final.assessments == []  # never returned on a guessed verdict
    assert "1 eligible candidates got no verdict" in caplog.text


def test_ranking_puts_experts_with_a_profile_ahead_of_those_without(spec):
    """Same LLM scores; the page-only person (no profile URL) loses NO_PROFILE_PENALTY points."""
    page_only = cand("former_employees:page:ann", "Ann Paged", method="llm_page",
                     positions=[pos("Kofax", end=(2025, 6), matched=TA)],
                     evidence=[ev("https://example.com/story", stype="web_page", etype="quote")])
    profiled = cand("former_employees:bob", "Bob Profiled", url="bob",
                    positions=[pos("Kofax", end=(2025, 6), matched=TA)])
    r = resolve(spec, cset(spec, [page_only, profiled]))
    llm = FakeLLM(by_model={RankingDraft: assess({"Ann Paged": (True, 5), "Bob Profiled": (True, 4)})})
    final = rank(spec, r, verify_all(spec, r), llm=llm, settings=settings())
    experts = next(g for g in final.groups if g.group_id == "former_employees").experts
    assert [e.name for e in experts] == ["Bob Profiled", "Ann Paged"]
    assert experts[1].profile_url is None and experts[1].score.profile_penalty == 25
    assert experts[0].score.profile_penalty == 0 and final.no_profile_penalty == 25
    off = rank(spec, r, verify_all(spec, r), llm=llm, settings=dataclasses.replace(settings(), no_profile_penalty=0))
    assert [e.name for e in next(g for g in off.groups if g.group_id == "former_employees").experts][0] == "Ann Paged"


def test_ranking_caps_the_list(spec):
    people = [cand(f"former_employees:p{i}", f"Person Number{i}", url=f"p{i}",
                   positions=[pos("Kofax", end=(2025, 1), matched=TA)]) for i in range(5)]
    r = resolve(spec, cset(spec, people))
    llm = FakeLLM(by_model={RankingDraft: assess({c.name: (True, 3) for c in people})})
    final = rank(spec, r, verify_all(spec, r), llm=llm, settings=settings(max_per_group=3))
    assert len(next(g for g in final.groups if g.group_id == "former_employees").experts) == 3


def test_ranking_keeps_candidates_judged_while_searching_beyond_the_pool(spec):
    """The pool caps new LLM verdicts only; people already judged (e.g. by the search gate) still rank."""
    people = [cand(f"former_employees:{i}", f"Person {i}", url=str(i),
                   positions=[pos("Kofax", end=(2025, 1), matched=TA)]) for i in range(5)]
    r = resolve(spec, cset(spec, people))
    llm = FakeLLM(by_model={RankingDraft: assess({c.name: (True, 3) for c in people})})
    cfg = dataclasses.replace(settings(), rank_pool_per_group=2)
    assessor = Assessor(spec, llm, cfg)
    group = spec.group("former_employees")
    assessor.qualified(group, r.for_group("former_employees"))  # the gate judges all five
    asked = assessor.asked
    final = rank(spec, r, verify_all(spec, r), llm=llm, settings=cfg, assessor=assessor)
    assert len(next(g for g in final.groups if g.group_id == "former_employees").experts) == 5
    assert assessor.asked == asked  # nothing re-asked


def test_pipeline_from_candidates_writes_the_final_files(spec, tmp_path):
    people = [cand("former_employees:a", "Ann Strong", url="a", positions=[pos("Kofax", end=(2026, 1), matched=TA)])]
    (tmp_path / pipeline.BRIEF_SPEC_FILE).write_text(spec.model_dump_json(), encoding="utf-8")
    (tmp_path / pipeline.CANDIDATES_FILE).write_text(cset(spec, people).model_dump_json(), encoding="utf-8")
    llm = FakeLLM(by_model={RankingDraft: assess({"Ann Strong": (True, 4)})})
    result = pipeline.run(tmp_path, settings=settings(), candidates_path=tmp_path / pipeline.CANDIDATES_FILE, llm=llm)
    assert result.metadata.stopped_after == "ranking"
    for name in ("resolved_candidates.json", "verification.json", "final_results.json", "final_results.md",
                 "final_results.csv", "verification.csv"):
        assert (tmp_path / name).exists(), name
    md = (tmp_path / "final_results.md").read_text(encoding="utf-8")
    assert "[Ann Strong](https://linkedin.com/in/a)" in md and "Ann Strong fits." in md
    assert result.metadata.counts["verification"]["eligible"] == 1


def test_pipeline_logs_each_stage_with_its_numbers(spec, tmp_path, caplog):
    people = [cand("former_employees:a", "Ann Strong", url="a", positions=[pos("Kofax", end=(2026, 1), matched=TA)])]
    (tmp_path / pipeline.BRIEF_SPEC_FILE).write_text(spec.model_dump_json(), encoding="utf-8")
    (tmp_path / pipeline.CANDIDATES_FILE).write_text(cset(spec, people).model_dump_json(), encoding="utf-8")
    llm = FakeLLM(by_model={RankingDraft: assess({"Ann Strong": (True, 4)})})
    with caplog.at_level("INFO", logger="sourcing_agent.progress"):
        pipeline.run(tmp_path, settings=settings(), candidates_path=tmp_path / pipeline.CANDIDATES_FILE, llm=llm)
    lines = [r.getMessage() for r in caplog.records if r.name == "sourcing_agent.progress"]
    assert lines[0].startswith("Run ") and lines[-1].startswith("Finished after ranking")
    for step in ("[6/9] profile_lookup done", "[7/9] entity_resolution done", "[8/9] verification done",
                 "[9/9] ranking done"):
        assert any(line.startswith(step) for line in lines), step
    assert any("former_employees: 1 eligible, 0 review, 0 ineligible" in line for line in lines)
    assert any("former_employees: 1 experts returned" in line for line in lines)


# --- profile lookup ------------------------------------------------------------ #

def _hit(url, title, snippet, qid="customers.lookup.001"):
    from sourcing_agent.search.base import canonical_url
    return SearchHit(hit_id=f"{qid}#1", query_id=qid, group_id="customers", provider="serper", query_type="people",
                     organisation=None, rank=1, page=1, title=title, url=url, canonical_url=canonical_url(url),
                     snippet=snippet, retrieved_at=NOW)


def test_lookup_accepts_only_same_name_with_the_organisation():
    from sourcing_agent.people_search.profile_lookup import matching_profiles
    right = _hit("https://www.linkedin.com/in/rduber", "Robert Duber - Director - University Hospitals | LinkedIn",
                 "Experience: University Hospitals · Cleveland")
    other_org = _hit("https://www.linkedin.com/in/rduber2", "Robert Duber - Plumber | LinkedIn", "Experience: Acme")
    other_name = _hit("https://www.linkedin.com/in/rdubin", "Robert Dubin - University Hospitals | LinkedIn", "")
    assert matching_profiles("Rob Duber", "University Hospitals", [right, other_org, other_name]) == [
        "https://linkedin.com/in/rduber"]
    twin = _hit("https://www.linkedin.com/in/robert-duber-9", "Robert Duber - University Hospitals | LinkedIn", "")
    assert len(matching_profiles("Rob Duber", "University Hospitals", [right, twin])) == 2  # ambiguous


class _LookupSerper:
    provider, cost_source = "serper", "test"

    def __init__(self, full_name_finds=True):
        self.queries, self.full_name_finds = [], full_name_finds

    def search(self, q):
        self.queries.append(q.query)
        if not self.full_name_finds and '"Rob Duber"' in q.query:  # LinkedIn says "Robert"
            return [], QueryRun(query_id=q.query_id, group_id=q.group_id, provider="serper", query_type="people",
                                query=q.query, requests=1, results=0, stop_reason="no_more_results",
                                duration_s=0, cost_usd=0.001)
        h = _hit("https://www.linkedin.com/in/rduber", "Robert Duber - Director - University Hospitals | LinkedIn",
                 "Experience: University Hospitals · Cleveland", qid=q.query_id)
        return [h], QueryRun(query_id=q.query_id, group_id=q.group_id, provider="serper", query_type="people",
                             query=q.query, requests=1, results=1, stop_reason="no_more_results", duration_s=0,
                             cost_usd=0.001)

    def close(self):
        pass


def test_profile_lookup_finds_links_and_merges_the_page_person(spec):
    from sourcing_agent.adaptive import RoundContext
    from sourcing_agent.people_search.profile_lookup import run_profile_lookup
    page = cand("customers:page:rob-duber-university-hospitals", "Rob Duber", group="customers", method="llm_page",
                positions=[Position(title="Manager, Process Improvement", organisation="University Hospitals",
                                    source="llm_page")],
                evidence=[ev("https://tungstenautomation.com/case-studies/uh", stype="web_page", etype="quote")])
    serper = _LookupSerper()
    ctx = RoundContext(spec=spec, settings=settings(), llm=FakeLLM(), clients={"serper": serper})
    lookups, cs = run_profile_lookup(ctx, cset(spec, [page]), {})
    assert serper.queries == ['site:linkedin.com/in "Rob Duber" University Hospitals']
    assert [x.outcome for x in lookups.lookups] == ["found"]
    record = next(c for c in cs.candidates if c.candidate_id == page.candidate_id)
    assert record.profile_lookup == "found" and record.same_person_as == ["customers:rduber"]
    r = resolve(spec, cs)
    merged = r.for_group("customers")
    assert len(merged) == 1 and merged[0].profile_url == "https://www.linkedin.com/in/rduber"
    assert any(e.source_url.endswith("/uh") for e in merged[0].evidence)  # the page quote is kept
    assert any(link.rule == "profile_lookup" for i in r.identities for link in i.links)
    again, _ = run_profile_lookup(ctx, cs, {})  # already looked up: no new searches
    assert again.lookups == [] and len(serper.queries) == 1


def test_profile_lookup_retries_unquoted_without_accents(spec):
    from sourcing_agent.adaptive import RoundContext
    from sourcing_agent.people_search.profile_lookup import run_profile_lookup
    page = cand("customers:page:rob-duber-uh", "Rob Duber", group="customers", method="llm_page",
                positions=[Position(title="Manager", organisation="University Hospitals", source="llm_page")],
                evidence=[ev("https://example.com/uh", stype="web_page", etype="quote")])
    serper = _LookupSerper(full_name_finds=False)
    ctx = RoundContext(spec=spec, settings=settings(), llm=FakeLLM(), clients={"serper": serper})
    lookups, _ = run_profile_lookup(ctx, cset(spec, [page]), {})
    assert serper.queries[1] == "site:linkedin.com/in Rob Duber University Hospitals"
    assert lookups.lookups[0].outcome == "found" and lookups.lookups[0].accepted_on == "search_result"


def test_lookup_query_drops_legal_form_and_accents():
    from sourcing_agent.extraction.orgs import fold
    from sourcing_agent.people_search.profile_lookup import query_organisation
    assert query_organisation("PCC Intermodal S.A.") == "PCC Intermodal"
    assert query_organisation("Euroterminal Sławków Sp. z o.o.") == "Euroterminal Sławków"
    assert query_organisation("CLIP Group") == "CLIP Group"
    assert fold("Wojciech Skrzypiński") == "Wojciech Skrzypinski"


class _NoOrgInSnippetSerper(_LookupSerper):
    """Two "Jakub Styczen" profiles; neither result names EUROTERMINAL (it is a past job)."""

    def search(self, q):
        self.queries.append(q.query)
        hits = [_hit("https://www.linkedin.com/in/jakub-ceo", "Jakub Styczeń - CEO / CCO | LinkedIn",
                     "CEO / CCO / Executive Manager | Operations & Logistics Director", qid=q.query_id),
                _hit("https://www.linkedin.com/in/jakub-student", "Jakub Styczeń - Student | LinkedIn",
                     "Student at a secondary school", qid=q.query_id)]
        for i, h in enumerate(hits, start=1):
            h.rank, h.hit_id = i, f"{q.query_id}#{i}"
        return hits, QueryRun(query_id=q.query_id, group_id=q.group_id, provider="serper", query_type="people",
                              query=q.query, requests=1, results=2, stop_reason="no_more_results", duration_s=0,
                              cost_usd=0.001)


def test_lookup_accepts_on_work_history_and_adds_only_that_profile(spec, monkeypatch):
    from sourcing_agent.adaptive import RoundContext
    from sourcing_agent.people_search import profile_lookup as lk
    page = cand("customers:page:jakub-euroterminal", "Jakub Styczeń", group="customers", method="llm_page",
                positions=[Position(title="Director", organisation="EUROTERMINAL Sławków", source="llm_page")],
                evidence=[ev("https://example.com/terminal", stype="web_page", etype="quote")])
    profiles = {
        "https://linkedin.com/in/jakub-ceo": [pos("Other Co", current=True),
                                              pos("Euroterminal Sławków Sp. z o.o.", end=(2023, 1))],
        "https://linkedin.com/in/jakub-student": [pos("I Liceum", current=True)],
    }

    def fake_extract(spec_, search, **kwargs):  # the profiles as Exa's work history would give them
        people = [cand(f"customers:{h.canonical_url.rsplit('/', 1)[-1]}", "Jakub Styczeń", group="customers",
                       url=h.canonical_url.rsplit("/", 1)[-1], positions=profiles[h.canonical_url])
                  for h in search.hits]
        return cset(spec_, people)

    monkeypatch.setattr(lk, "extract_candidates", fake_extract)
    ctx = RoundContext(spec=spec, settings=settings(), llm=FakeLLM(), clients={"serper": _NoOrgInSnippetSerper()})
    lookups, cs = lk.run_profile_lookup(ctx, cset(spec, [page]), {})
    x = lookups.lookups[0]
    assert (x.outcome, x.accepted_on, x.matches) == ("found", "work_history", ["https://linkedin.com/in/jakub-ceo"])
    assert sorted(c.candidate_id for c in cs.candidates) == ["customers:jakub-ceo", page.candidate_id]


def test_work_history_must_name_the_organisation_itself():
    from sourcing_agent.people_search.profile_lookup import worked_at
    person = cand("g:x", "Mirosław Łapot", url="x", positions=[pos("Baltic Rail", current=True),
                                                               pos("DB Schenker Rail Polska", end=(2011, 8))])
    assert not worked_at(person, "Rail Polska")  # a longer name containing it is another company
    terminal = cand("g:y", "Jakub Styczeń", url="y", positions=[pos("EUROTERMINAL SŁAWKÓW SP. Z O.O.", end=(2025, 11))])
    assert worked_at(terminal, "EUROTERMINAL Sławków")  # legal form and case differ only


def test_lookup_profile_without_work_history_needs_its_text_to_name_the_organisation(spec, monkeypatch):
    from sourcing_agent.adaptive import RoundContext
    from sourcing_agent.extraction.enrichment import PageContent
    from sourcing_agent.people_search import profile_lookup as lk
    page = cand("customers:page:jakub-euroterminal", "Jakub Styczeń", group="customers", method="llm_page",
                positions=[Position(title="Director", organisation="EUROTERMINAL Sławków", source="llm_page")],
                evidence=[ev("https://example.com/terminal", stype="web_page", etype="quote")])
    texts = {"https://linkedin.com/in/jakub-ceo": "Posts: talk to me about the Euroterminal Sławków terminal",
             "https://linkedin.com/in/jakub-student": "Student, likes trains"}
    monkeypatch.setattr(lk, "_read_profiles", lambda ctx, urls: {
        u: PageContent(url=u, title=None, text=t, published_date=None) for u, t in texts.items()})
    monkeypatch.setattr(lk, "extract_candidates", lambda spec_, search, **kw: cset(spec_, [
        cand(f"customers:{h.canonical_url.rsplit('/', 1)[-1]}", "Jakub Styczeń", group="customers",
             url=h.canonical_url.rsplit("/", 1)[-1]) for h in search.hits]))  # records with no work history
    ctx = RoundContext(spec=spec, settings=settings(), llm=FakeLLM(), clients={"serper": _NoOrgInSnippetSerper()})
    x = lk.run_profile_lookup(ctx, cset(spec, [page]), {})[0].lookups[0]
    assert (x.outcome, x.accepted_on, x.matches) == ("found", "profile_text", ["https://linkedin.com/in/jakub-ceo"])


def test_rerun_with_nothing_to_look_up_keeps_the_lookup_file(spec, tmp_path):
    page = cand("customers:page:rob-duber-uh", "Rob Duber", group="customers", method="llm_page",
                positions=[Position(title="Manager", organisation="University Hospitals", source="llm_page")],
                evidence=[ev("https://example.com/uh", stype="web_page", etype="quote")])
    (tmp_path / pipeline.BRIEF_SPEC_FILE).write_text(spec.model_dump_json(), encoding="utf-8")
    (tmp_path / pipeline.CANDIDATES_FILE).write_text(cset(spec, [page]).model_dump_json(), encoding="utf-8")
    serper = _LookupSerper()
    for _ in range(2):
        pipeline.run(tmp_path, settings=settings(), candidates_path=tmp_path / pipeline.CANDIDATES_FILE,
                     llm=FakeLLM(), search_clients={"serper": serper}, stop_after="profile_lookup")
    saved = json.loads((tmp_path / pipeline.PROFILE_LOOKUP_FILE).read_text(encoding="utf-8"))
    assert [x["outcome"] for x in saved["lookups"]] == ["found"] and len(serper.queries) == 1


def test_page_quote_stays_in_the_dossier_after_merging_a_long_profile():
    from sourcing_agent.ranking.dossier import evidence_items
    jobs = [ev(f"https://linkedin.com/in/x#{i}", claim=f"job {i}") for i in range(8)]
    quote = Evidence(source_url="https://example.com/case", source_type="web_page", provider="exa", retrieved_at=NOW,
                     claim="Quoted on the case study", supporting_text="We run TotalAgility", evidence_type="quote",
                     supports=["identity", "role", "group_relevance"])
    c = cand("customers:x", "X Y", group="customers", url="x", evidence=[*jobs, quote])
    assert evidence_items(c)[0] is quote


def test_evaluation_counts_only_labelled_rows():
    from sourcing_agent.evaluation import evaluate
    rows = [
        {"group_id": "g", "identity_id": "a", "overall_eligible": "yes", "exclusions_honoured": "yes",
         "evidence_supports_claim": "yes"},
        {"group_id": "g", "identity_id": "b", "overall_eligible": "no", "exclusions_honoured": "no",
         "evidence_supports_claim": ""},
        {"group_id": "g", "identity_id": "b", "overall_eligible": "", "exclusions_honoured": "",
         "evidence_supports_claim": ""},
    ]
    m = evaluate(rows)["groups"]["g"]
    assert (m["precision"]["yes_rate"], m["precision"]["n"]) == (0.5, 2)
    assert (m["exclusion_violation_rate"]["value"], m["exclusion_violation_rate"]["n"]) == (0.5, 2)
    assert (m["critical_claim_support_rate"]["yes_rate"], m["critical_claim_support_rate"]["n"]) == (1.0, 1)
    assert m["duplicate_rate"]["value"] == round(1 / 3, 3)


def test_evaluation_interval_is_honest_about_small_samples():
    from sourcing_agent.evaluation import evaluate, wilson
    assert wilson(20, 20) == [0.839, 1.0]  # 20 of 20: very likely above ~84%, not "100%"
    assert wilson(59, 59)[0] == 0.939
    rows = [{"group_id": "g", "identity_id": "a", "overall_eligible": "unknown", "exclusions_honoured": "",
             "evidence_supports_claim": ""}]
    p = evaluate(rows)["groups"]["g"]["precision"]
    assert p["n"] == 0 and p["unknown"] == 1 and p["ci95"] is None


def test_nickname_with_same_surname_and_employer_merges_page_records(spec):
    a = cand("customers:page:robert", "Robert Duber", group="customers", method="llm_page",
             positions=[pos("University Hospitals", start=None, end=None)])
    b = cand("customers:page:rob", "Rob Duber", group="customers", method="llm_page",
             positions=[pos("University Hospitals, Portage Medical Center", start=None, end=None)])
    assert resolve(spec, cset(spec, [a, b])).stats.identities == 1


def test_display_quotes_drop_markdown_and_contact_details():
    from sourcing_agent.ranking.dossier import clean_quote
    assert clean_quote("### VP - [Kofax](https://x.y/z)\nSep 2022") == "VP - Kofax Sep 2022"
    assert clean_quote("Call 510-205-6511 or a@b.com, 2019-2024") == "Call [phone removed] or [email removed], 2019-2024"


def test_unclear_candidates_are_listed_for_review_not_returned(spec, tmp_path):
    people = [
        cand("former_employees:a", "Ann Clear", url="a", positions=[pos("Kofax", end=(2026, 1), matched=TA)]),
        cand("former_employees:b", "Ben Unclear", url="b", positions=[pos("Kofax", end=(2025, 1), matched=TA)]),
    ]
    r = resolve(spec, cset(spec, people))
    v = verify_all(spec, r)
    llm = FakeLLM(by_model={RankingDraft: assess({"Ann Clear": ("meets", 4), "Ben Unclear": ("unclear", 4)})})
    final = rank(spec, r, v, llm=llm, settings=settings())
    g = next(g for g in final.groups if g.group_id == "former_employees")
    assert [e.name for e in g.experts] == ["Ann Clear"]
    assert [e.name for e in g.for_review] == ["Ben Unclear"] and g.unclear == 1 and g.rejected_by_ranking == 0
    from sourcing_agent.reporting.shortlist_files import final_markdown, final_rows
    md = final_markdown(final, r)
    assert "Borderline, for researcher review" in md and "Ben Unclear" in md
    assert [row["name"] for row in final_rows(final)] == ["Ann Clear"]


def test_a_rejected_api_key_stops_ranking_instead_of_skipping_candidates(spec):
    from sourcing_agent.search.base import InvalidCredentials

    def reject(user):
        raise InvalidCredentials("Anthropic", "HTTP 401")
    person = cand("former_employees:a", "Ann Strong", url="a", positions=[pos("Kofax", end=(2026, 1), matched=TA)])
    r = resolve(spec, cset(spec, [person]))
    with pytest.raises(InvalidCredentials):
        rank(spec, r, verify_all(spec, r), llm=FakeLLM(by_model={RankingDraft: reject}), settings=settings())

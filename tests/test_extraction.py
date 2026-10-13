from __future__ import annotations

import json
from datetime import date, datetime, timezone

import httpx
import pytest

from sourcing_agent import pipeline
from sourcing_agent.brief.parser import build_spec
from sourcing_agent.config import ExtractionSettings, LLMPricing, SearchSettings, Settings
from sourcing_agent.extraction.candidate_extractor import extract_candidates
from sourcing_agent.extraction.enrichment import ContentsResult, ExaContents, PageContent
from sourcing_agent.extraction.orgs import matching_organisations, name_matches
from sourcing_agent.extraction.pages import extract_from_page
from sourcing_agent.extraction.profiles import candidate_from_profile
from sourcing_agent.metrics import RunTracker
from sourcing_agent.models import (
    GeneratorInfo,
    Organisation,
    PageExtractionDraft,
    PageOrganisationDraft,
    PagePersonDraft,
    ParserInfo,
    ProfileDraft,
    ProfilePositionDraft,
    SearchConfig,
    SearchHit,
    SearchResults,
)

from .conftest import FakeLLM

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
TUNGSTEN = Organisation(name="Tungsten Automation", aliases=["Kofax"])
ORGS = [TUNGSTEN, Organisation(name="OpenText", aliases=[]), Organisation(name="IBM", aliases=[])]
GEN = GeneratorInfo(model="fake", effort="medium", generated_at=NOW)


def hit(url="https://www.linkedin.com/in/brad", group="former_employees", provider="serper", qtype="people",
        qid="former_employees.people.serper.01", rank=1, **extra) -> SearchHit:
    from sourcing_agent.search.base import canonical_url
    return SearchHit(
        hit_id=f"{qid}#{rank}", query_id=qid, group_id=group, provider=provider, query_type=qtype,
        organisation=None, rank=rank, page=0, title=extra.pop("title", "Brad Hamilton - Retired | LinkedIn"),
        url=url, canonical_url=canonical_url(url), snippet=extra.pop("snippet", "Experience: Kofax · Location: Irvine"),
        retrieved_at=NOW, **extra,
    )


PERSON = {"type": "person", "properties": {"name": "Brad Hamilton", "location": "Lake Forest, CA", "workHistory": [
    {"title": "Retired", "company": {"name": "Retired"}, "dates": {"from": "2026-01-01", "to": None}},
    {"title": "EVP, R&D", "company": {"name": "Tungsten Automation"}, "dates": {"from": "2022-09-01", "to": "2026-01-01"}},
    {"title": "SVP Product Development", "company": {"name": "Kofax Inc."}, "dates": {"from": "2015-01-01", "to": "2022-08-01"}},
]}}
PROFILE_TEXT = "# Brad Hamilton\n\nRetired - Former EVP\n\n## Experience\n\n### EVP, R&D - [Tungsten Automation](x)\nSep 2022 - Jan 2026"


# --- organisation matching -------------------------------------------------- #

@pytest.mark.parametrize("org, employer, expected", [
    ("Kofax", "Kofax India Pvt. Ltd.", True),
    ("Kofax", "Tungsten Automation, a Kofax company", True),
    ("OpenText", "Open Text Corporation", True),          # adjacent tokens joined
    ("Tungsten Automation", "Tungsten Network", False),   # partial name is not a match
    ("IBM", "Mibmax Solutions", False),                   # no substring matching
    ("IBM", "IBM Consulting", True),
    ("PKP Cargo", "PKP Cargo S.A.", True),
    ("PKP Cargo", "PKP Intercity", False),
])
def test_name_matches(org, employer, expected):
    assert name_matches(org, employer) is expected


def test_matching_organisations_returns_canonical_names():
    assert matching_organisations("Kofax UK Ltd", ORGS) == ["Tungsten Automation"]


# --- profiles --------------------------------------------------------------- #

def test_profile_from_exa_entity_is_parsed_by_code():
    content = PageContent(url="https://linkedin.com/in/brad", title="Brad Hamilton", text=PROFILE_TEXT,
                          published_date="2026-09-08", entities=[PERSON])
    cand, warnings = candidate_from_profile(_group(), ORGS, [hit()], content, llm=None)

    assert cand.extraction_method == "exa_entities" and warnings == []
    assert cand.name == "Brad Hamilton" and cand.location == "Lake Forest, CA"
    assert cand.headline == "Retired - Former EVP"
    assert cand.candidate_id == "former_employees:brad"
    retired, evp, svp = cand.positions
    assert retired.is_current is True and retired.matched_organisations == []
    assert evp.start == date(2022, 9, 1) and evp.end == date(2026, 1, 1) and evp.is_current is False
    assert evp.matched_organisations == ["Tungsten Automation"]
    assert svp.matched_organisations == ["Tungsten Automation"]  # "Kofax Inc." via alias
    # Identity + one structured evidence item per matched position.
    kinds = [(e.evidence_type, e.supports) for e in cand.evidence]
    assert kinds[0] == ("snippet", ["identity"])
    assert [k for k in kinds[1:]] == [("provider_structured", ["employment", "dates", "role"])] * 2
    assert cand.evidence[1].supporting_text == "### EVP, R&D - [Tungsten Automation](x)"
    assert cand.data_as_of == "2026-09-08"


def test_snippet_only_profile_keeps_status_unknown():
    cand, warnings = candidate_from_profile(_group(), ORGS, [hit()], None, llm=None)
    assert cand.extraction_method == "snippet_only"
    assert cand.name == "Brad Hamilton" and cand.headline == "Retired"
    assert [(p.organisation, p.is_current, p.matched_organisations) for p in cand.positions] == [
        ("Kofax", None, ["Tungsten Automation"])
    ]
    assert cand.evidence[1].evidence_type == "snippet"
    assert "search snippet" in warnings[0]


def test_llm_profile_fallback_drops_positions_with_unverifiable_quotes():
    draft = ProfileDraft(name="Brad Hamilton", headline="Former EVP", positions=[
        ProfilePositionDraft(title="EVP, R&D", organisation="Tungsten Automation", start="2022-09", end="2026-01",
                             quote="EVP, R&D - [Tungsten Automation](x) Sep 2022 - Jan 2026"),
        ProfilePositionDraft(title="CEO", organisation="Kofax", start="2010-01", end="present",
                             quote="CEO at Kofax since 2010"),  # invented: not in the text
    ])
    content = PageContent(url="https://linkedin.com/in/brad", title=None, text=PROFILE_TEXT, published_date=None)
    cand, warnings = candidate_from_profile(_group(), ORGS, [hit()], content, llm=FakeLLM(draft))

    assert cand.extraction_method == "llm_profile_text"
    assert [p.title for p in cand.positions] == ["EVP, R&D"]
    assert cand.positions[0].start == date(2022, 9, 1)
    assert any("unverifiable quote" in w for w in warnings)
    assert cand.evidence[1].evidence_type == "quote"


# --- pages ------------------------------------------------------------------ #

PAGE = PageContent(
    url="https://example.com/tui-case-study", title="TUI case study", published_date="2024-03-01",
    text="TUI Group runs Kofax ReadSoft for 5M invoices a year. \"Accuracy went up,\" said Jane Doe, "
         "Head of Accounts Payable at TUI Group.",
)


def test_page_extraction_keeps_only_quote_backed_items(tungsten_spec):
    draft = PageExtractionDraft(
        people=[
            PagePersonDraft(name="Jane Doe", title="Head of Accounts Payable", organisation="TUI Group",
                            relevance="Leads AP at a named Kofax customer.",
                            quote="said Jane Doe, Head of Accounts Payable at TUI Group"),
            PagePersonDraft(name="John Smith", title="CIO", organisation="TUI Group",
                            relevance="Invented", quote="John Smith, CIO"),
        ],
        organisations=[
            PageOrganisationDraft(name="TUI Group", relationship="runs Kofax ReadSoft for invoices",
                                  quote="TUI Group runs Kofax ReadSoft for 5M invoices a year."),
            PageOrganisationDraft(name="Acme", relationship="customer", quote="Acme is a customer"),
        ],
    )
    group = tungsten_spec.group("customers")
    people, orgs, warnings = extract_from_page(
        tungsten_spec, group, ORGS, PAGE, FakeLLM(draft), hit_ids=["h1"], query_ids=["q1"], providers=["serper"],
    )
    assert [p.name for p in people] == ["Jane Doe"]
    jane = people[0]
    assert jane.candidate_id == "customers:page:jane_doe-tui_group"
    assert jane.extraction_method == "llm_page" and jane.profile_url is None
    assert jane.evidence[0].evidence_type == "quote" and jane.evidence[0].source_url == PAGE.url
    assert [o.name for o in orgs] == ["TUI Group"]
    assert len(warnings) == 2


def test_person_whose_name_is_not_on_the_page_is_dropped(tungsten_spec):
    draft = PageExtractionDraft(people=[PagePersonDraft(
        name="Janet Dough", title=None, organisation="TUI Group", relevance="x",
        quote="Head of Accounts Payable at TUI Group")], organisations=[])
    people, _, warnings = extract_from_page(
        tungsten_spec, tungsten_spec.group("customers"), ORGS, PAGE, FakeLLM(draft),
        hit_ids=[], query_ids=[], providers=[],
    )
    assert people == [] and "name not found" in warnings[0]


# --- stage ------------------------------------------------------------------- #

class FakeContents:
    def __init__(self, pages: dict[str, PageContent]):
        self.pages, self.asked = pages, []

    def fetch(self, urls):
        from sourcing_agent.search.base import canonical_url
        self.asked += urls
        found = {canonical_url(u): self.pages[u] for u in urls if u in self.pages}
        return ContentsResult(pages=found, requested=len(urls), failed=[u for u in urls if u not in self.pages],
                              requests=1, cost_usd=0.001 * len(found))

    def close(self):
        pass


def test_stage_enriches_serper_hits_merges_and_records_cost(tungsten_spec):
    serper_url = "https://www.linkedin.com/in/brad"
    exa_hit = hit("https://linkedin.com/in/kim", provider="exa", qid="former_employees.people.exa.01",
                  title="Kim Holm", text=PROFILE_TEXT.replace("Brad Hamilton", "Kim Holm"),
                  entities=[{**PERSON, "properties": {**PERSON["properties"], "name": "Kim Holm"}}])
    search = _results([
        hit(serper_url, rank=1),
        hit(serper_url.upper().replace("HTTPS://WWW.", "https://uk."), rank=3, qid="former_employees.people.serper.02"),
        exa_hit,
        hit("https://example.com/tui-case-study", group="customers", qtype="company", qid="customers.company.serper.01"),
        hit("https://example.com/other", group="customers", qtype="company", qid="customers.company.serper.01", rank=2),
    ], tungsten_spec)
    contents = FakeContents({
        serper_url: PageContent(url=serper_url, title=None, text=PROFILE_TEXT, published_date=None, entities=[PERSON]),
        "https://example.com/tui-case-study": PAGE,
    })
    page_draft = PageExtractionDraft(
        people=[PagePersonDraft(name="Jane Doe", title="Head of AP", organisation="TUI Group", relevance="r",
                                quote="said Jane Doe, Head of Accounts Payable at TUI Group")],
        organisations=[])
    settings = _settings(max_pages=1)
    tracker = RunTracker("t", RUN_DATE, LLMPricing(None, None, "t"))

    cs = extract_candidates(tungsten_spec, search, llm=FakeLLM(page_draft), settings=settings,
                            extractor=GEN, contents=contents, tracker=tracker)

    # Serper hits for the same profile (www + uk subdomain) merge; Exa content isn't re-fetched.
    assert contents.asked == [serper_url, "https://example.com/tui-case-study"]
    ids = sorted(c.candidate_id for c in cs.candidates)
    assert ids == ["customers:page:jane_doe-tui_group", "former_employees:brad", "former_employees:kim"]
    brad = next(c for c in cs.candidates if c.candidate_id == "former_employees:brad")
    assert len(brad.source_hit_ids) == 2 and brad.extraction_method == "exa_entities"
    assert any("best-ranked 1" in w for w in cs.warnings)  # page cap applied
    rows = {r.group_id: r for r in cs.summary}
    assert rows["former_employees"].candidates == 2 and rows["former_employees"].with_matched_position == 2
    assert cs.enrichment.succeeded == 2 and cs.enrichment.cost_usd == pytest.approx(0.002)
    usage = tracker.meta.search_usage[0]
    assert usage.purpose == "contents" and usage.results == 2


def test_exa_contents_client_batches_and_reports_failures():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={
            "costDollars": {"total": 0.001},
            "results": [{"url": body["urls"][0], "title": "ok", "text": "hello", "entities": []}],
        })

    client = ExaContents("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    urls = [f"https://a.com/{i}" for i in range(150)]
    out = client.fetch(urls + urls[:5])  # duplicates are fetched once
    assert len(seen) == 2 and len(seen[0]["urls"]) == 100 and len(seen[1]["urls"]) == 50
    assert seen[0]["text"] == {"maxCharacters": 4000}
    assert len(out.pages) == 2 and len(out.failed) == 148 and out.cost_usd == pytest.approx(0.002)


def test_exa_contents_records_an_unreadable_response_as_failed_pages():
    client = ExaContents("k", client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>gateway error</html>"))))
    out = client.fetch(["https://a.com/1", "https://a.com/2"])
    assert out.pages == {} and len(out.failed) == 2 and "unreadable" in out.errors[0]


def test_pipeline_extraction_only_from_saved_search_results(tmp_path, tungsten_spec):
    search = _results([hit()], tungsten_spec)
    (tmp_path / pipeline.BRIEF_SPEC_FILE).write_text(tungsten_spec.model_dump_json(), encoding="utf-8")
    (tmp_path / pipeline.SEARCH_RESULTS_FILE).write_text(search.model_dump_json(), encoding="utf-8")
    settings = _settings(enrich=False)

    result = pipeline.run(tmp_path, settings=settings, search_path=tmp_path / pipeline.SEARCH_RESULTS_FILE,
                          llm=FakeLLM(), stop_after="candidate_extraction")
    saved = pipeline.load_candidates(tmp_path / pipeline.CANDIDATES_FILE)
    assert [c.extraction_method for c in saved.candidates] == ["snippet_only"]
    assert result.metadata.stopped_after == "candidate_extraction"
    assert pipeline.load_organisations(tmp_path / pipeline.ORGANISATIONS_FILE).organisations == []


# --- helpers ----------------------------------------------------------------- #

RUN_DATE = date(2026, 10, 10)


@pytest.fixture
def tungsten_spec(tungsten_draft, tungsten_text):
    return build_spec(tungsten_draft, text=tungsten_text, brief_id="tungsten_automation", source_path="b.md",
                      run_date=RUN_DATE, parser=ParserInfo(model="f", effort="medium", parsed_at=NOW))


def _group():
    from sourcing_agent.models import ExpertGroup

    from .conftest import _group as draft_group
    return ExpertGroup(**draft_group(group_id="former_employees", name="Former employees").model_dump())


def _results(hits, spec) -> SearchResults:
    return SearchResults(
        brief_id=spec.brief_id, source_sha256=spec.source.sha256, executed_at=NOW,
        config=SearchConfig(serper_max_pages=5, exa_num_results=50, exa_text_max_chars=4000),
        summary=[], runs=[], hits=hits,
    )


def _settings(max_pages=60, enrich=True) -> Settings:
    return Settings(
        llm_model="fake", llm_effort="medium", anthropic_api_key=None, llm_pricing=LLMPricing(0.1, 0.5, "t"),
        search=SearchSettings(exa_api_key=None),
        extraction=ExtractionSettings(max_pages_per_group=max_pages, llm_workers=2, enrich=enrich),
    )


def test_placeholder_organisation_not_named_on_page_is_dropped(tungsten_spec):
    draft = PageExtractionDraft(people=[], organisations=[PageOrganisationDraft(
        name="IM (infrastructure manager, not named on the page)", relationship="manages track",
        quote="TUI Group runs Kofax ReadSoft")])
    _, orgs, warnings = extract_from_page(
        tungsten_spec, tungsten_spec.group("customers"), ORGS, PAGE, FakeLLM(draft),
        hit_ids=[], query_ids=[], providers=[],
    )
    assert orgs == [] and "dropped organisation" in warnings[0]


def test_legal_form_variants_share_keys():
    from sourcing_agent.extraction.candidate_extractor import _merge_organisations
    from sourcing_agent.extraction.orgs import org_key
    from sourcing_agent.models import DiscoveredOrganisation

    assert org_key("CLIP Group S.A.") == org_key("CLIP Group") == "clip group"
    merged = _merge_organisations([
        DiscoveredOrganisation(group_id="g", name="PKP Polskie Linie Kolejowe S.A.", relationship="a", evidence=[]),
        DiscoveredOrganisation(group_id="g", name="PKP Polskie Linie Kolejowe", relationship="b", evidence=[]),
    ])
    assert len(merged) == 1


@pytest.mark.parametrize("name, key", [
    ("CLIP Group S.A.", "clip group"),
    ("Q4Rail Sp. z o.o.", "q4rail"),
    ("LOKOFRACHT sp. z o.o. sp. k.", "lokofracht"),
    ("Kofax India Pvt. Ltd.", "kofax india"),
    ("Plan B", "plan b"),  # a real single-letter word is kept
])
def test_org_key_strips_legal_forms(name, key):
    from sourcing_agent.extraction.orgs import org_key
    assert org_key(name) == key


# --- customer organisations ----------------------------------------------------- #

@pytest.mark.parametrize("name, placeholder", [
    ("A major European insurance company (unnamed)", True),
    ("Regional US bank", True),
    ("Leading insurer (unnamed on the page)", True),
    ("insurance company", True),
    ("Aviva", False),
    ("The Hertz Corporation", False),
    ("eBay", False),
])
def test_placeholder_organisations(name, placeholder):
    from sourcing_agent.extraction.orgs import is_placeholder_org
    assert is_placeholder_org(name) is placeholder


def test_org_name_cleaning_and_ampersand_variants():
    from sourcing_agent.extraction.orgs import clean_org_name, org_key
    assert clean_org_name("Cerner, now Oracle Health") == "Cerner"
    assert clean_org_name("Stater (formerly Hypotheekbank)") == "Stater"
    assert org_key("W.R. Grace & Co") == org_key("W.R. Grace and Company") == "w r grace"
    assert org_key("Procter & Gamble") == "procter and gamble"


def test_page_organisations_skip_the_target_and_descriptions(tungsten_spec):
    draft = PageExtractionDraft(people=[], organisations=[
        PageOrganisationDraft(name="Tungsten Automation", relationship="vendor", quote="TUI Group runs Kofax ReadSoft"),
        PageOrganisationDraft(name="TUI Group, now TUI AG", relationship="runs ReadSoft", quote="TUI Group runs Kofax ReadSoft"),
    ])
    _, orgs, _ = extract_from_page(
        tungsten_spec, tungsten_spec.group("customers"), ORGS, PAGE, FakeLLM(draft),
        hit_ids=[], query_ids=[], providers=[],
    )
    assert [o.name for o in orgs] == ["TUI Group"]


def test_people_quoted_on_pages_are_matched_to_discovered_customers(tungsten_spec):
    from sourcing_agent.extraction.candidate_extractor import _tag_discovered_organisations
    from sourcing_agent.models import Candidate, DiscoveredOrganisation, Position

    person = Candidate(candidate_id="customers:page:anne", group_id="customers", name="Anne", profile_url=None,
                       canonical_url=None, extraction_method="llm_page",
                       positions=[Position(title="Fleet Accounting Manager", organisation="Hertz Australia",
                                           source="llm_page")])
    org = DiscoveredOrganisation(group_id="customers", name="Hertz Australia", relationship="customer", evidence=[])
    _tag_discovered_organisations(tungsten_spec, [person], [org])
    assert person.positions[0].matched_organisations == ["Hertz Australia"]


def test_customer_status_uses_discovered_organisations(tungsten_spec):
    from sourcing_agent.verification.employment import departure_status, relevant_organisations

    group = tungsten_spec.group("customers")
    assert relevant_organisations(tungsten_spec, group) == {"Tungsten Automation"}
    orgs = relevant_organisations(tungsten_spec, group, ["Serco"])
    assert orgs == {"Serco"}
    from sourcing_agent.models import Candidate, Position
    c = Candidate(candidate_id="x", group_id="customers", name="x", profile_url=None, canonical_url=None,
                  extraction_method="exa_entities", positions=[Position(
                      title="RPA lead", organisation="Serco Group plc", is_current=True, date_precision="month",
                      matched_organisations=["Serco"], source="exa_entities")])
    assert departure_status(c, group, orgs) == "still_current"


def test_organisation_people_stage_from_reviewed_organisations(tmp_path, tungsten_spec):
    from sourcing_agent.extraction.candidate_extractor import organisation_set
    from sourcing_agent.models import CandidateSet, DiscoveredOrganisation, EnrichmentStats, GeneratorInfo, QueryRun

    spec = tungsten_spec.model_copy(deep=True)
    spec.group("customers").eligible_organisation_types = ["Enterprises running Tungsten Automation"]
    orgs = [DiscoveredOrganisation(group_id="customers", name=n, relationship="customer", evidence=[])
            for n in ("Serco", "Aviva")]
    base = CandidateSet(brief_id=spec.brief_id, source_sha256=spec.source.sha256, extracted_at=NOW,
                        extractor=GeneratorInfo(model="f", effort="m", generated_at=NOW), summary=[],
                        enrichment=EnrichmentStats(), candidates=[], organisations=orgs)
    reviewed = organisation_set(spec, base)
    next(o for o in reviewed.organisations if o.name == "Aviva").include = False   # a reviewer drops Aviva
    (tmp_path / pipeline.BRIEF_SPEC_FILE).write_text(spec.model_dump_json(), encoding="utf-8")
    (tmp_path / pipeline.CANDIDATES_FILE).write_text(base.model_dump_json(), encoding="utf-8")
    (tmp_path / pipeline.ORGANISATIONS_FILE).write_text(reviewed.model_dump_json(), encoding="utf-8")

    class FakeSerper:
        provider, cost_source = "serper", "test"

        def __init__(self):
            self.queries = []

        def search(self, q):
            self.queries.append(q.query)
            h = hit("https://www.linkedin.com/in/serco-rpa", group="customers", provider="serper",
                    qid=q.query_id, title="Jo Bloggs - RPA lead - Serco | LinkedIn")
            return [h], QueryRun(query_id=q.query_id, group_id=q.group_id, provider="serper", query_type="people",
                                 query=q.query, requests=1, results=1, stop_reason="no_more_results",
                                 duration_s=0, cost_usd=0.001)

    serper = FakeSerper()
    result = pipeline.run(tmp_path, settings=_settings(enrich=False), llm=FakeLLM(),
                          organisations_path=tmp_path / pipeline.ORGANISATIONS_FILE,
                          search_clients={"serper": serper}, stop_after="organisation_people")
    assert len(serper.queries) == 1 and '"Serco"' in serper.queries[0]
    assert result.metadata.stopped_after == "organisation_people"
    saved = pipeline.load_candidates(tmp_path / pipeline.CANDIDATES_FILE)
    assert [c.name for c in saved.for_group("customers")] == ["Jo Bloggs"]
    assert (tmp_path / pipeline.ORG_PEOPLE_QUERIES_FILE).exists()


def test_organisations_excluded_only_for_current_staff_stay_discoverable(tungsten_spec):
    # Tungsten Automation is excluded for current employees only (former staff are a group).
    from sourcing_agent.extraction.pages import _not_a_discovery
    from sourcing_agent.models import Organisation, OrganisationExclusion

    group = tungsten_spec.group("customers")
    assert _not_a_discovery(tungsten_spec, group, "Tungsten Automation")       # the target itself
    spec = tungsten_spec.model_copy(deep=True)
    spec.target_organisation = None
    assert not _not_a_discovery(spec, group, "Kofax")                            # current-only exclusion
    spec.excluded_organisations.append(OrganisationExclusion(
        organisation=Organisation(name="UTK", aliases=[]), scope="current_and_former_employees",
        applies_to_group_ids=[], source_quote="x"))
    assert _not_a_discovery(spec, group, "UTK")


@pytest.mark.parametrize("org, other, matches, same", [
    ("UTK", "Urząd Transportu Kolejowego", True, True),
    ("UTK", "Urzędu Transportu Kolejowego", True, True),          # Polish case ending
    ("PKP PLK", "PKP Polskie Linie Kolejowe S.A.", True, True),
    ("PKP PLK", "PKP Polskich Linii Kolejowych", True, True),
    ("PKP PLK", "PKP Polskie Linie Kolejowe (PKP PLK)", True, True),
    ("PKP Cargo", "PKP CARGO S.A.", True, True),
    ("PKP Cargo", "PKP Cargo Terminale Sp. z o.o.", True, False),  # a subsidiary: matches, but not the same
    ("IBM", "Mibmax", False, False),
    ("UTK", "Office of Rail Transport", False, False),
])
def test_acronyms_and_same_organisation(org, other, matches, same):
    from sourcing_agent.extraction.orgs import name_matches, same_organisation
    assert name_matches(org, other) is matches
    assert same_organisation(org, other) is same


def test_discovered_variants_fold_into_the_briefs_organisation(tungsten_spec):
    from sourcing_agent.extraction.candidate_extractor import _merge_organisations, canonicalise_organisations
    from sourcing_agent.models import DiscoveredOrganisation

    found = [DiscoveredOrganisation(group_id="customers", name=n, relationship="r", evidence=[])
             for n in ("Tungsten Automation Inc.", "Tungsten Automation India", "Aviva")]
    merged = _merge_organisations(canonicalise_organisations(tungsten_spec, found))
    assert sorted(o.name for o in merged) == ["Aviva", "Tungsten Automation", "Tungsten Automation India"]

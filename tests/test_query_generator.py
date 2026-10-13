from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from sourcing_agent import cli, pipeline
from sourcing_agent.brief.parser import build_spec
from sourcing_agent.config import LLMPricing, Settings, _int_env
from sourcing_agent.models import (
    BriefSpec,
    CompanySearchDraft,
    GeneratorInfo,
    ParserInfo,
    QueryDraft,
    SearchTermsDraft,
)
from sourcing_agent.search.company_queries import build_company_queries, people_markers
from sourcing_agent.search.query_generator import build_user_prompt, generate_queries
from sourcing_agent.search.scope import target_if_group_is_relative_to_it

from .conftest import FakeLLM

RUN_DATE = date(2026, 10, 10)
GEN = GeneratorInfo(model="fake", effort="medium", generated_at=datetime(2026, 10, 10, tzinfo=timezone.utc))
SETTINGS = Settings(
    llm_model="fake", llm_effort="medium", anthropic_api_key=None,
    llm_pricing=LLMPricing(0.10, 0.50, source="test"), queries_per_provider=2,
)


@pytest.fixture
def spec(tungsten_draft, tungsten_text) -> BriefSpec:
    # Groups: former_employees (named org, former), customers (no named orgs),
    # competitors (5 named orgs, current_or_former).
    return build_spec(
        tungsten_draft, text=tungsten_text, brief_id="tungsten_automation", source_path="brief.md",
        run_date=RUN_DATE,
        parser=ParserInfo(model="fake", effort="medium", parsed_at=datetime(2026, 10, 10, tzinfo=timezone.utc)),
    )


def q(text: str, lang: str = "en") -> QueryDraft:
    return QueryDraft(query=text, language=lang, intent="customer evidence")


def company_draft(serper: list[str], exa: list[str], lang: str = "en") -> CompanySearchDraft:
    return CompanySearchDraft(
        role_keywords=["head of automation", "IT director"], topic_keywords=["document capture"], title_keywords=[],
        serper=[q(t, lang) for t in serper], exa=[q(t, lang) for t in exa], customer_story_urls=[],
    )


TERMS = SearchTermsDraft(
    role_keywords=["product", " Product ", "partnerships", '"go-to-market"'],
    topic_keywords=["document capture"],
    title_keywords=[],
)
COMPANIES = company_draft(
    serper=['"Kofax" customer case study', '"Tungsten Automation" selects invoice processing'],
    exa=["Bank that deployed Kofax capture for loan documents",
         "Press release: insurer goes live with Tungsten Automation"],
)


def fake_llm() -> FakeLLM:
    return FakeLLM(TERMS, COMPANIES)


# --- whole stage ----------------------------------------------------------- #

# Excluding people currently at Tungsten Automation / Kofax (single-word title
# exclusions only: a negated multi-word title phrase returns nothing on Google).
TUNGSTEN_STAFF = (
    '-intitle:tungsten -intitle:kofax -"at Tungsten Automation" -"at Kofax" -"at Tungsten" '
    '-"Tungsten Automation · Present" -"Kofax · Present"'
)
DIR = "-site:linkedin.com/in/dir"


def test_named_former_group_gets_quarter_function_and_alias_slices(spec):
    llm = fake_llm()
    plan = generate_queries(spec, llm=llm, n=5, xray_n=20, generator=GEN)

    g = plan.group("former_employees")
    assert g.mode == "people"
    serper = [x.query for x in plan.for_group("former_employees", "serper")]
    assert len(serper) == 20
    # Departure quarters (window Oct 2023 - Apr 2026, newest first) alternate with function groups.
    assert serper[0] == (
        f'site:linkedin.com/in "Tungsten Automation" ("Jan 2026" OR "Feb 2026" OR "Mar 2026" OR "Apr 2026") '
        f"{TUNGSTEN_STAFF} {DIR}"
    )
    assert serper[1] == f'site:linkedin.com/in "Tungsten Automation" ("channel" OR "partner" OR "alliances") {TUNGSTEN_STAFF} {DIR}'
    assert serper[2].startswith('site:linkedin.com/in "Tungsten Automation" ("Oct 2025" OR "Nov 2025" OR "Dec 2025")')
    # The former name, with the window's earliest years and with employee titles.
    assert f'site:linkedin.com/in "Kofax" ("2023" OR "2024") "Tungsten Automation" {TUNGSTEN_STAFF} {DIR}' in serper
    assert any(x.startswith('site:linkedin.com/in "Kofax" ("account executive"') for x in serper)
    # All ten quarters fit under the cap, including the oldest.
    assert sum('2023" OR' in x or '2024" OR' in x or '2025" OR' in x or '2026" OR' in x for x in serper) >= 10
    assert any('"Oct 2023"' in x for x in serper)
    # No products, no country filters, no plain "Kofax" slice (mostly skill-listers).
    assert not any("TotalAgility" in x or "uk.linkedin" in x for x in serper)
    assert f'site:linkedin.com/in "Kofax" {TUNGSTEN_STAFF} {DIR}' not in serper
    exa = [x.query for x in plan.for_group("former_employees", "exa")]
    assert exa == [
        "professional profiles of former employees of Tungsten Automation (also known as Kofax) "
        "who no longer work there and left between October 2023 and April 2026",
        "former Kofax product managers, consultants and customer success managers now at another company",
    ]
    assert {x.organisation for x in g.queries} == {"Tungsten Automation"}
    # Terms for both named groups, company discovery for customers.
    assert sorted(c["output_model"].__name__ for c in llm.calls) == [
        "CompanySearchDraft", "SearchTermsDraft", "SearchTermsDraft",
    ]


def test_query_caps_per_group(spec):
    plan = generate_queries(spec, llm=fake_llm(), n=2, xray_n=3, generator=GEN)
    for g in plan.groups:
        assert len(plan.for_group(g.group_id, "exa")) <= 2
        assert len(plan.for_group(g.group_id, "serper", "people")) <= 3
        assert len(plan.for_group(g.group_id, "serper", "company")) <= 2
    assert plan.queries_per_provider == 2 and plan.xray_queries_per_group == 3
    # The most productive mix comes first when the cap bites.
    assert [x.intent.split("(")[-1] for x in plan.for_group("former_employees", "serper")] == [
        "name + Jan 2026-Apr 2026)", "name + channel/partner)", "name + Oct 2025-Dec 2025)",
    ]


def test_named_current_or_former_group_gets_one_query_per_org(spec):
    plan = generate_queries(spec, llm=fake_llm(), n=5, xray_n=5, generator=GEN)

    g = plan.group("competitors")
    assert g.role_keywords == ["product", "partnerships", "go-to-market"]  # cleaned, deduped
    serper = plan.for_group("competitors", "serper")
    roles = '("product" OR "partnerships" OR "go-to-market")'
    assert [x.organisation for x in serper] == ["ABBYY", "Hyland", "OpenText", "IBM", "Microsoft"]
    # Current or former staff: the plain name (not intitle), minus Tungsten's current staff.
    assert serper[0].query == f'site:linkedin.com/in "ABBYY" {roles} {TUNGSTEN_STAFF} {DIR}'
    exa = plan.for_group("competitors", "exa")
    assert len(exa) == 5
    exa_ibm = next(x for x in exa if x.organisation == "IBM")
    assert "left after October 2024" in exa_ibm.query


def test_more_organisations_than_queries_share_queries(spec):
    plan = generate_queries(spec, llm=fake_llm(), n=2, xray_n=2, generator=GEN)
    serper = plan.for_group("competitors", "serper")
    assert [x.organisation for x in serper] == ["ABBYY / Hyland / OpenText", "IBM / Microsoft"]
    assert serper[1].query.startswith('site:linkedin.com/in ("IBM" OR "Microsoft") ')
    exa = plan.for_group("competitors", "exa")
    assert "employees of IBM or Microsoft" in exa[1].query
    assert any("5 organisations for 2 queries per provider" in w for w in plan.validation_warnings)


def test_group_without_named_orgs_gets_company_queries_only(spec):
    plan = generate_queries(spec, llm=fake_llm(), n=2, xray_n=20, generator=GEN)

    g = plan.group("customers")
    assert g.mode == "companies_then_people"
    assert {x.query_type for x in g.queries} == {"company"}
    assert len(plan.for_group("customers", "serper")) == 2
    assert len(plan.for_group("customers", "exa")) == 2
    assert g.role_keywords == ["head of automation", "IT director"]  # kept for the people step
    assert plan.queries_per_provider == 2


def test_user_prompt_contains_group_and_exclusions_but_not_computed_dates(spec):
    prompt = build_user_prompt(spec, spec.group("customers"), "task")
    assert prompt.startswith("task")
    assert '"group_id": "customers"' in prompt
    assert "current_employees" in prompt
    assert "departure_dates" not in prompt
    assert "screening_questions" not in prompt


# --- company query checks -------------------------------------------------- #

def customers_relative_to_target(spec):
    return spec.group("customers").model_copy(
        update={"eligible_organisation_types": ["Enterprises running Kofax capture at volume"]}
    )


def test_company_queries_are_normalised_and_counted(spec):
    draft = COMPANIES.model_copy(deep=True)
    draft.serper[0].query = '  "Kofax"   customer\n case study '
    draft.serper.append(q("Kofax award winner shared services"))
    draft.exa = draft.exa[:1]
    queries, warnings = build_company_queries(spec, spec.group("customers"), draft, n=2)

    assert queries[0].query_id == "customers.company.serper.01"
    assert queries[0].query == '"Kofax" customer case study'
    assert any("serper: model returned 3 usable queries, kept first 2" in w for w in warnings)
    assert any("exa: only 1 usable queries (wanted 2)" in w for w in warnings)


def test_near_duplicate_company_queries_dropped_ignoring_operators(spec):
    draft = COMPANIES.model_copy(deep=True)
    draft.serper.insert(1, q("Kofax customer case study site:kofax.com"))
    queries, warnings = build_company_queries(spec, spec.group("customers"), draft, n=2)
    assert [x.query for x in queries if x.provider == "serper"] == [
        '"Kofax" customer case study', '"Tungsten Automation" selects invoice processing',
    ]
    assert any("near-duplicate" in w for w in warnings)


def test_company_queries_must_name_target_when_group_is_relative_to_it(spec):
    group = customers_relative_to_target(spec)
    assert target_if_group_is_relative_to_it(spec, group).name == "Tungsten Automation"
    assert target_if_group_is_relative_to_it(spec, spec.group("customers")) is None  # no types given

    draft = COMPANIES.model_copy(deep=True)
    draft.exa[1].query = "Insurer describing its document capture rollout"
    _, warnings = build_company_queries(spec, group, draft, n=2)
    assert warnings == [
        "customers.company.exa.02: does not name the target (Tungsten Automation): "
        "'Insurer describing its document capture rollout'"
    ]


@pytest.mark.parametrize("query, expected", [
    # Real company "queries" from the first Polish run that were really people searches.
    ('"PKP PLK" "train path allocation" former head', ["former"]),
    ("byli pracownicy Departamentu Planowania Sieci PKP PLK", ["byli", "pracownicy"]),
    ('site:linkedin.com "przewoźnik kolejowy" "dyrektor operacyjny" były', ["linkedin.com", "były"]),
    ("conference speaker biography of a former operations director", ["speaker", "biography", "former"]),
    # Genuine company discovery, and Polish words that merely look similar.
    ('"Kofax" customer case study enterprise document capture', []),
    ("nowy terminal intermodalny uruchomiony 2024 OR 2025 Polska", []),
    ("byłoby Bytom intermodal", []),
])
def test_people_markers(query, expected):
    assert people_markers(query) == expected


def test_people_search_disguised_as_company_query_is_flagged(spec):
    draft = COMPANIES.model_copy(deep=True)
    draft.exa[0].query = "Profile of a former Kofax customer IT director"
    _, warnings = build_company_queries(spec, customers_relative_to_target(spec), draft, n=2)
    assert any(w.startswith("customers.company.exa.01: looks like a people search") for w in warnings)


def test_company_query_language_coverage(spec):
    group = spec.group("customers").model_copy(update={"query_languages": ["en", "pl"]})
    draft = COMPANIES.model_copy(deep=True)
    draft.exa[1].language = "de"
    _, warnings = build_company_queries(spec, group, draft, n=2)
    assert "customers/company/serper: no queries in group language 'pl'" in warnings
    assert "customers/company/exa: no queries in group language 'pl'" in warnings
    assert any("unexpected language 'de'" in w for w in warnings)


# --- pipeline / CLI / config ----------------------------------------------- #

def test_pipeline_from_saved_spec_skips_parsing(tmp_path, spec):
    spec_dir = tmp_path / "reviewed"
    spec_dir.mkdir()
    spec_path = spec_dir / "brief_spec.json"
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    out = tmp_path / "out"

    llm = fake_llm()  # has no BriefDraft response: parsing would fail the test
    result = pipeline.run(out, settings=SETTINGS, spec_path=spec_path, llm=llm, stop_after="query_generation")

    assert result.metadata.run_date == RUN_DATE
    assert pipeline.load_brief_spec(out / pipeline.BRIEF_SPEC_FILE) == spec
    plan = pipeline.load_query_plan(out / pipeline.QUERIES_FILE)
    assert [g.mode for g in plan.groups] == ["people", "companies_then_people", "people"]
    meta = json.loads((out / pipeline.RUN_METADATA_FILE).read_text(encoding="utf-8"))
    assert [s["stage"] for s in meta["stages"]] == ["query_generation"]
    assert meta["stopped_after"] == "query_generation"

    # A second run in the same folder keeps the first run's record.
    pipeline.run(out, settings=SETTINGS, spec_path=out / pipeline.BRIEF_SPEC_FILE, llm=llm,
                 stop_after="query_generation")
    history = (out / pipeline.RUN_HISTORY_FILE).read_text(encoding="utf-8").splitlines()
    assert len(history) == 2
    assert json.loads(history[0])["run_id"] == meta["run_id"]


def test_pipeline_requires_exactly_one_source(tmp_path):
    with pytest.raises(ValueError):
        pipeline.run(tmp_path, settings=SETTINGS)


def test_cli_rejects_run_date_with_spec(tmp_path, spec):
    spec_path = tmp_path / "brief_spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    assert cli.main(["--spec", str(spec_path), "--run-date", "2026-01-01"]) == 2


@pytest.mark.parametrize("raw, ok", [("5", True), ("20", True), ("0", False), ("21", False), ("x", False)])
def test_queries_per_provider_env_validation(monkeypatch, raw, ok):
    monkeypatch.setenv("QUERIES_PER_PROVIDER", raw)
    if ok:
        assert _int_env("QUERIES_PER_PROVIDER", 5, 1, 20) == int(raw)
    else:
        with pytest.raises(ValueError):
            _int_env("QUERIES_PER_PROVIDER", 5, 1, 20)


# --- search terms ------------------------------------------------------------ #

def test_terms_request_names_every_organisation(spec):
    llm = fake_llm()
    generate_queries(spec, llm=llm, n=2, xray_n=20, generator=GEN)
    competitors_call = next(c for c in llm.calls if '"group_id": "competitors"' in c["user"])
    assert competitors_call["user"].startswith(
        "Supply role_keywords, topic_keywords and title_keywords for people at these organisations: "
        "ABBYY, Hyland, OpenText, IBM, Microsoft."
    )


def test_missing_role_keywords_are_warned(spec):
    from sourcing_agent.search.query_generator import clean_terms

    warnings: list[str] = []
    clean_terms(SearchTermsDraft(role_keywords=[], topic_keywords=[], title_keywords=[]), spec.group("competitors"), warnings)
    assert warnings == ["competitors: model gave no role_keywords"]


# --- customers of the target -------------------------------------------------- #

def test_customer_groups_get_customer_story_templates_instead_of_llm_google_queries(spec):
    from sourcing_agent.search.company_queries import customer_story_queries

    draft = COMPANIES.model_copy(deep=True, update={"customer_story_urls": ["https://www.tungstenautomation.com/case-studies/"]})
    queries, _ = build_company_queries(spec, customers_relative_to_target(spec), draft, n=5)
    google = [x.query for x in queries if x.provider == "serper"]
    assert google == [
        "site:tungstenautomation.com/case-studies",
        "site:featuredcustomers.com/vendor/tungsten-automation",
        "site:casestudies.com/company/tungsten-automation",
        '("Tungsten Automation" OR "Kofax") ("case study" OR "customer story" OR "success story")',
    ]
    assert [x.query for x in queries if x.provider == "exa"] == [d.query for d in COMPANIES.exa]
    target = spec.target_organisation
    assert len(customer_story_queries("g", target, [], 2)) == 2


def test_customer_story_section_is_discovered_from_search_results():
    from sourcing_agent.models import Organisation
    from sourcing_agent.search.company_queries import discover_customer_story_sites

    results = {
        '"Tungsten Automation" ("case studies" OR "customer stories" OR "success stories")': [
            "https://www.tungstenautomation.com/case-studies",
            "https://www.tungstendemocenter.com/download/x.pdf",          # not the target's own domain
            "https://www.featuredcustomers.com/vendor/tungsten-automation/case-studies",  # aggregator
            "https://www.tungstenautomation.com/industries/public-sector",  # not a story section
            "https://www.tungstenautomation.de/case-studies/twinings",
        ],
        '"Kofax" ("case studies" OR "customer stories" OR "success stories")': [
            "https://www.tungstenautomation.com/case-studies/region-stockholm",
            "https://www.erpresearch.com/erp-add-ons/ocr/tungsten-kofax/case-studies",
        ],
    }
    calls = []

    def fake_search(q):
        calls.append(q)
        return results[q]

    sites = discover_customer_story_sites(Organisation(name="Tungsten Automation", aliases=["Kofax"]), fake_search)
    assert sites == ["tungstenautomation.com/case-studies", "tungstenautomation.de/case-studies"]
    assert len(calls) == 2


def test_customer_discovery_falls_back_to_llm_paths_and_warns_when_none(spec):
    from sourcing_agent.search.query_generator import plan_group

    group = customers_relative_to_target(spec)
    plan, warnings = plan_group(spec, group, FakeLLM(COMPANIES), 5, 20, site_search=lambda q: [])
    assert any("found no customer-story section" in w for w in warnings)
    assert plan.queries[0].query == "site:featuredcustomers.com/vendor/tungsten-automation"


def test_organisation_people_queries_one_per_org_requiring_the_target(spec):
    from sourcing_agent.models import Organisation
    from sourcing_agent.search.people_plan import build_organisation_people_queries

    orgs = [Organisation(name=n, aliases=[]) for n in ("Serco", "Aviva", "Henkel")]
    queries = build_organisation_people_queries(
        spec, customers_relative_to_target(spec), orgs, TERMS, max_xray=2, max_exa=5,
    )
    assert [x.organisation for x in queries] == ["Serco", "Aviva"]          # capped, best first
    assert all(x.provider == "serper" for x in queries)                     # no Exa for customer groups
    assert queries[0].query == (
        'site:linkedin.com/in "Serco" ("Tungsten Automation" OR "Kofax") '
        '-"at Tungsten Automation" -"at Kofax" -"Tungsten Automation · Present" -"Kofax · Present" '
        "-site:linkedin.com/in/dir"
    )
    assert "-intitle:" not in queries[0].query   # would drop customer staff titled "Kofax developer"


def test_organisation_people_for_former_groups_exclude_current_staff(spec):
    from sourcing_agent.models import Organisation
    from sourcing_agent.search.people_plan import build_organisation_people_queries

    group = spec.group("customers").model_copy(update={"required_employment_status": "former"})
    queries = build_organisation_people_queries(
        spec, group, [Organisation(name="Lotos Kolej", aliases=[])],
        SearchTermsDraft(role_keywords=["product", "partnerships", "go-to-market"], topic_keywords=[],
                         title_keywords=[]),
        max_xray=5, max_exa=1,
    )
    google = queries[0].query
    assert google.startswith('site:linkedin.com/in "Lotos Kolej" ("product" OR "partnerships" OR "go-to-market")')
    assert '-intitle:lotos -"at Lotos Kolej"' in google and '-"Lotos Kolej · Present"' in google
    assert [q.provider for q in queries] == ["serper", "exa"]   # Exa kept for groups not defined by a target

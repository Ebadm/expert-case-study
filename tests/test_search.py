from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from sourcing_agent import pipeline
from sourcing_agent.config import LLMPricing, SearchSettings, Settings
from sourcing_agent.metrics import RunTracker
from sourcing_agent.models import (
    GeneratorInfo,
    GroupQueryPlan,
    QueryPlan,
    QueryRun,
    SearchHit,
    SearchQuery,
    SearchTermsDraft,
)
from sourcing_agent.search.base import canonical_url, clean_text, is_junk_url, is_linkedin_profile, keep_result
from sourcing_agent.search.exa import ExaSearch
from sourcing_agent.search.runner import run_searches
from sourcing_agent.search.serper import SerperSearch

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def sq(provider="serper", query_type="people", qid=None, language="en") -> SearchQuery:
    return SearchQuery(
        query_id=qid or f"g.{query_type}.{provider}.01", group_id="g", provider=provider,
        query_type=query_type, query="site:linkedin.com/in \"Kofax\"", language=language,
        intent="test", organisation="Kofax" if query_type == "people" else None,
    )


def serper_page(urls: list[str]) -> dict:
    return {
        "searchParameters": {"q": "x", "type": "search", "engine": "google"},
        "organic": [
            {"title": f"<b>Person</b> {u}", "link": u, "snippet": "Ex-Kofax &amp; more. Aug 2021 - Dec 2025",
             "position": i + 1, "date": "Mar 10, 2025"}
            for i, u in enumerate(urls)
        ],
        "credits": 1,
    }


def serper_with_pages(pages: dict[int, dict | httpx.Response], **kwargs):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"body": body, "headers": request.headers})
        page = pages.get(body["page"], {"organic": [], "credits": 1})
        return page if isinstance(page, httpx.Response) else httpx.Response(200, json=page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SerperSearch("key", client=client, **{"price_per_1k": 1.0, **kwargs}), calls


def profiles(start: int, stop: int) -> list[str]:
    return [f"https://www.linkedin.com/in/p{i}" for i in range(start, stop)]


# --- helpers ----------------------------------------------------------------- #

@pytest.mark.parametrize("url, expected", [
    ("https://www.linkedin.com/in/Jane-Doe/?trk=abc", "https://linkedin.com/in/jane-doe"),
    ("https://uk.linkedin.com/in/jane-doe", "https://linkedin.com/in/jane-doe"),
    ("https://de.linkedin.com/in/kimbex/en", "https://linkedin.com/in/kimbex"),  # language variant
    ("http://WWW.Example.com/Case-Study/#top", "https://example.com/Case-Study"),
    ("https://example.com", "https://example.com/"),
])
def test_canonical_url(url, expected):
    assert canonical_url(url) == expected


def test_clean_text_strips_tags_and_entities():
    assert clean_text("Ex-<strong>Kofax</strong> &amp;  more ") == "Ex-Kofax & more"
    assert clean_text(None) is None
    assert clean_text("<b></b>") is None


@pytest.mark.parametrize("url, profile", [
    ("https://www.linkedin.com/in/jane-doe", True),
    ("https://de.linkedin.com/in/kimbex/en", True),
    ("https://www.linkedin.com/in/dir/jane/doe", False),     # name directory
    ("https://www.linkedin.com/pub/dir/Jane/Doe", False),
    ("https://www.linkedin.com/posts/jane-doe_kofax-123", False),
    ("https://www.linkedin.com/company/kofax", False),
    ("https://notlinkedin.com/in/jane", False),
])
def test_is_linkedin_profile(url, profile):
    assert is_linkedin_profile(url) is profile


def test_keep_result_by_query_type():
    assert keep_result("https://www.linkedin.com/in/jane", "people")
    assert not keep_result("https://www.tungstenautomation.com/case-study", "people")
    assert keep_result("https://www.tungstenautomation.com/case-study", "company")
    for junk in ("https://exa.ai/library/publication/1", "https://uk.indeed.com/viewjob?jk=1",
                 "https://www.linkedin.com/jobs/view/123"):
        assert is_junk_url(junk) and not keep_result(junk, "company")


# --- Serper ------------------------------------------------------------------ #

def test_serper_paginates_until_a_page_only_repeats():
    serper, calls = serper_with_pages(
        {1: serper_page(profiles(0, 10)), 2: serper_page(profiles(5, 15)), 3: serper_page(profiles(5, 15))},
        max_pages=5,
    )
    hits, run = serper.search(sq())

    assert [c["body"]["page"] for c in calls] == [1, 2, 3]
    assert run.stop_reason == "repeated_page"
    assert run.requests == 3 and run.results == 15 == len(hits)
    assert [h.rank for h in hits] == list(range(1, 16))
    assert hits[10].page == 2
    assert run.cost_usd == pytest.approx(3 * 1.0 / 1000)  # 3 credits


def test_serper_request_shape_and_normalisation():
    serper, calls = serper_with_pages({1: serper_page(["https://pl.linkedin.com/in/x"])})
    hits, run = serper.search(sq(language="pl"))

    assert calls[0]["headers"]["X-API-KEY"] == "key"
    # No country filter; language only for non-English queries; num > 10 is refused on free plans.
    assert calls[0]["body"] == {"q": 'site:linkedin.com/in "Kofax"', "page": 1, "hl": "pl"}
    assert run.stop_reason == "no_more_results"  # a short page is the last page
    h = hits[0]
    assert h.title == "Person https://pl.linkedin.com/in/x"
    assert h.snippet == "Ex-Kofax & more. Aug 2021 - Dec 2025"
    assert h.canonical_url == "https://linkedin.com/in/x"
    assert h.published_date == "Mar 10, 2025"
    assert h.hit_id == "g.people.serper.01#1"


def test_serper_people_queries_keep_only_profiles():
    urls = ["https://www.linkedin.com/in/a", "https://www.linkedin.com/in/dir/x/y",
            "https://www.linkedin.com/posts/a_1", "https://www.linkedin.com/in/b", *profiles(0, 6)]
    serper, _ = serper_with_pages({1: serper_page(urls)}, max_pages=1)
    hits, run = serper.search(sq())
    assert [h.url for h in hits][:2] == ["https://www.linkedin.com/in/a", "https://www.linkedin.com/in/b"]
    assert run.filtered == 2 and len(hits) == 8


def test_serper_stops_on_empty_page_and_max_pages():
    serper, _ = serper_with_pages({1: serper_page(profiles(0, 10))})
    assert serper.search(sq())[1].stop_reason == "empty_page"

    serper, calls = serper_with_pages({1: serper_page(profiles(0, 10))}, max_pages=1)
    assert serper.search(sq())[1].stop_reason == "max_pages"
    assert len(calls) == 1


def test_serper_people_queries_have_their_own_page_cap():
    pages = {i: serper_page(profiles(10 * i, 10 * i + 10)) for i in range(1, 6)}
    serper, calls = serper_with_pages(pages, max_pages=5, people_max_pages=2)
    hits, run = serper.search(sq())
    assert len(calls) == 2 and len(hits) == 20 and run.stop_reason == "max_pages"

    serper, calls = serper_with_pages({i: serper_page([f"https://a.com/{10 * i + j}" for j in range(10)])
                                       for i in range(1, 6)}, max_pages=5, people_max_pages=2)
    serper.search(sq(query_type="company"))
    assert len(calls) == 5  # company queries keep the general cap


def test_serper_query_can_start_later_and_cap_its_own_pages():
    pages = {i: serper_page(profiles(10 * i, 10 * i + 10)) for i in range(1, 6)}
    serper, calls = serper_with_pages(pages, max_pages=5, people_max_pages=3)
    hits, run = serper.search(sq().model_copy(update={"max_pages": 1}))
    assert [c["body"]["page"] for c in calls] == [1] and run.stop_reason == "max_pages"

    serper, calls = serper_with_pages(pages, max_pages=5, people_max_pages=3)
    hits, run = serper.search(sq(qid="g.people.serper.01+p2").model_copy(update={"first_page": 2}))
    assert [c["body"]["page"] for c in calls] == [2, 3]  # up to the people cap
    assert hits[0].rank == 11 and hits[0].hit_id == "g.people.serper.01+p2#11" and run.requests == 2


def test_serper_retries_429_then_succeeds():
    responses = iter([
        httpx.Response(429, headers={"retry-after": "0"}, text="slow down"),
        httpx.Response(200, json=serper_page(["https://www.linkedin.com/in/a"])),
    ])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: next(responses)))
    hits, run = SerperSearch("key", client=client).search(sq())
    assert len(hits) == 1 and run.errors == []


def test_serper_plan_error_is_recorded_not_raised():
    serper, _ = serper_with_pages({1: httpx.Response(400, json={
        "message": "Query pattern not allowed for free accounts.", "statusCode": 400})})
    hits, run = serper.search(sq())
    assert hits == [] and run.stop_reason == "error"
    assert "HTTP 400" in run.errors[0] and "not allowed" in run.errors[0]


# --- Exa --------------------------------------------------------------------- #

EXA_RESPONSE = {
    "requestId": "r1",
    "costDollars": {"total": 0.022},
    "results": [
        {"title": "Brad Hamilton", "url": "https://linkedin.com/in/brad", "publishedDate": "2026-09-08T00:00:00Z",
         "text": "# Brad Hamilton\n## Experience", "highlights": ["EVP R&D - Tungsten Automation", "Kofax"],
         "entities": [{"type": "person", "properties": {"workHistory": [
             {"title": "EVP", "company": {"name": "Tungsten Automation"},
              "dates": {"from": "2022-09-01", "to": "2026-01-01"}}]}}]},
        {"title": "Dup", "url": "https://www.linkedin.com/in/brad/"},
    ],
}


def exa_client(response: httpx.Response):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return response

    return ExaSearch("key", client=httpx.Client(transport=httpx.MockTransport(handler))), bodies


def test_exa_people_query_body_and_parsing():
    exa, bodies = exa_client(httpx.Response(200, json=EXA_RESPONSE))
    hits, run = exa.search(sq("exa", "people"))

    body = bodies[0]
    assert body["category"] == "people" and body["type"] == "auto" and body["numResults"] == 50
    assert body["contents"]["text"] == {"maxCharacters": 4000}
    # The people category rejects these with a 400.
    assert not {"startPublishedDate", "endPublishedDate", "excludeDomains"} & body.keys()

    assert run.cost_usd == 0.022 and run.stop_reason == "single_request" and run.requests == 1
    assert len(hits) == 1  # the www./trailing-slash duplicate is dropped
    h = hits[0]
    assert h.snippet == "EVP R&D - Tungsten Automation" and h.extra_snippets == ["Kofax"]
    assert h.entities[0]["properties"]["workHistory"][0]["dates"]["to"] == "2026-01-01"
    assert h.text.startswith("# Brad Hamilton")


def test_exa_company_query_sends_no_category_and_uses_company_search_type():
    exa, bodies = exa_client(httpx.Response(200, json={"results": [], "costDollars": {"total": 0.005}}))
    exa.search(sq("exa", "company"))
    assert "category" not in bodies[0]
    assert bodies[0]["type"] == "deep"


def test_exa_synthetic_library_pages_are_filtered():
    response = {"costDollars": {"total": 0.01}, "results": [
        {"title": "Using Kofax Solutions", "url": "https://exa.ai/library/publication/1bf95x4mss9"},
        {"title": "intermodal terminals in Poland", "url": "https://exa.ai/library/places?q=521994b028370e8d"},
        {"title": "Real case study", "url": "https://www.tungstenautomation.com/learn/case-studies/x"},
    ]}
    exa, _ = exa_client(httpx.Response(200, json=response))
    hits, run = exa.search(sq("exa", "company"))
    assert [h.url for h in hits] == ["https://www.tungstenautomation.com/learn/case-studies/x"]
    assert run.filtered == 2 and hits[0].rank == 1


def test_exa_people_queries_keep_only_profiles():
    response = {"costDollars": {"total": 0.01}, "results": [
        {"title": "Brad", "url": "https://linkedin.com/in/brad"},
        {"title": "Kofax company page", "url": "https://www.linkedin.com/company/kofax"},
        {"title": "Article", "url": "https://example.com/kofax-news"},
    ]}
    exa, _ = exa_client(httpx.Response(200, json=response))
    hits, run = exa.search(sq("exa", "people"))
    assert [h.url for h in hits] == ["https://linkedin.com/in/brad"] and run.filtered == 2


def test_exa_error_is_recorded():
    exa, _ = exa_client(httpx.Response(400, json={"error": "bad filter"}))
    hits, run = exa.search(sq("exa", "people"))
    assert hits == [] and run.stop_reason == "error" and "HTTP 400" in run.errors[0]


# --- runner / pipeline ------------------------------------------------------- #

class FakeClient:
    def __init__(self, provider: str, n: int = 2, cost: float | None = 0.01):
        self.provider, self.n, self.cost = provider, n, cost
        self.cost_source = "test"
        self.seen: list[str] = []

    def search(self, q: SearchQuery):
        self.seen.append(q.query_id)
        hits = [
            SearchHit(
                hit_id=f"{q.query_id}#{i}", query_id=q.query_id, group_id=q.group_id, provider=q.provider,
                query_type=q.query_type, organisation=q.organisation, rank=i, page=None,
                title=f"t{i}", url=f"https://linkedin.com/in/shared{i}", canonical_url=f"https://linkedin.com/in/shared{i}",
                snippet=None, retrieved_at=NOW,
            )
            for i in range(1, self.n + 1)
        ]
        run = QueryRun(
            query_id=q.query_id, group_id=q.group_id, provider=q.provider, query_type=q.query_type,
            query=q.query, requests=1, results=self.n, stop_reason="single_request", duration_s=0.0,
            cost_usd=self.cost,
        )
        return hits, run


def plan_with(queries: list[SearchQuery]) -> QueryPlan:
    return QueryPlan(
        brief_id="b", source_sha256="sha", spec_parsed_at=NOW,
        generator=GeneratorInfo(model="fake", effort="medium", generated_at=NOW),
        queries_per_provider=5,
        groups=[GroupQueryPlan(group_id="g", mode="people", queries=queries)],
    )


def test_runner_collects_hits_in_plan_order_and_records_usage():
    queries = [sq("serper", qid="g.people.serper.01"), sq("exa", qid="g.people.exa.01"),
               sq("serper", qid="g.people.serper.02")]
    serper, exa = FakeClient("serper"), FakeClient("exa", cost=0.022)
    tracker = RunTracker("b", NOW.date(), LLMPricing(None, None, "test"))
    results = run_searches(plan_with(queries), SearchSettings(), clients={"serper": serper, "exa": exa},
                           tracker=tracker)

    assert [r.query_id for r in results.runs] == ["g.people.serper.01", "g.people.exa.01", "g.people.serper.02"]
    assert results.hits[0].query_id == "g.people.serper.01"
    rows = {(r.provider, r.query_type): r for r in results.summary}
    assert rows[("serper", "people")].queries == 2 and rows[("serper", "people")].hits == 4
    assert rows[("serper", "people")].unique_urls == 2 and rows[("serper", "people")].linkedin_profiles == 2
    usage = {u.provider: u for u in tracker.meta.search_usage}
    assert usage["serper"].requests == 2 and usage["exa"].cost_usd == 0.022


def test_runner_records_missing_api_key_as_errors():
    results = run_searches(plan_with([sq("exa")]), SearchSettings(), clients={})
    assert results.hits == []
    assert results.runs[0].errors == ["EXA_API_KEY not set"]
    assert results.summary[0].errors == 1


def test_pipeline_search_only_from_saved_queries(tmp_path, tungsten_draft, tungsten_text):
    from sourcing_agent.brief.parser import build_spec
    from sourcing_agent.models import ParserInfo

    spec = build_spec(
        tungsten_draft, text=tungsten_text, brief_id="tungsten_automation", source_path="b.md",
        run_date=NOW.date(), parser=ParserInfo(model="f", effort="medium", parsed_at=NOW),
    )
    plan = plan_with([sq("serper"), sq("exa", qid="g.people.exa.01")]).model_copy(
        update={"brief_id": spec.brief_id, "source_sha256": spec.source.sha256}
    )
    src = tmp_path / "in"
    src.mkdir()
    (src / pipeline.BRIEF_SPEC_FILE).write_text(spec.model_dump_json(), encoding="utf-8")
    (src / pipeline.QUERIES_FILE).write_text(plan.model_dump_json(), encoding="utf-8")
    settings = Settings(llm_model="fake", llm_effort="medium", anthropic_api_key=None,
                        llm_pricing=LLMPricing(0.1, 0.5, "test"))

    result = pipeline.run(
        src, settings=settings, queries_path=src / pipeline.QUERIES_FILE,
        search_clients={"serper": FakeClient("serper"), "exa": FakeClient("exa")}, stop_after="search",
    )

    # The Exa people query is held back as a top-up for groups Google leaves short.
    saved = pipeline.load_search_results(src / pipeline.SEARCH_RESULTS_FILE)
    assert len(saved.hits) == 2 == len(result.search.hits)
    assert [r.query_id for r in saved.runs] == ["g.people.serper.01"]
    meta = json.loads((src / pipeline.RUN_METADATA_FILE).read_text(encoding="utf-8"))
    assert [s["stage"] for s in meta["stages"]] == ["search"]
    assert meta["llm_calls"] == [] and meta["estimated_search_cost_usd"] == 0.01
    assert meta["estimated_total_cost_usd"] == 0.01

    # Without a target everything runs in the search stage.
    no_target = Settings(llm_model="fake", llm_effort="medium", anthropic_api_key=None,
                         llm_pricing=LLMPricing(0.1, 0.5, "test"), target_candidates_per_group=0)
    result = pipeline.run(
        tmp_path / "all", settings=no_target, queries_path=src / pipeline.QUERIES_FILE,
        search_clients={"serper": FakeClient("serper"), "exa": FakeClient("exa")}, stop_after="search",
    )
    assert len(result.search.hits) == 4


def test_pipeline_rejects_queries_from_a_different_brief(tmp_path, tungsten_draft, tungsten_text):
    from sourcing_agent.brief.parser import build_spec
    from sourcing_agent.models import ParserInfo

    spec = build_spec(
        tungsten_draft, text=tungsten_text, brief_id="t", source_path="b.md",
        run_date=NOW.date(), parser=ParserInfo(model="f", effort="medium", parsed_at=NOW),
    )
    (tmp_path / pipeline.BRIEF_SPEC_FILE).write_text(spec.model_dump_json(), encoding="utf-8")
    (tmp_path / pipeline.QUERIES_FILE).write_text(plan_with([sq()]).model_dump_json(), encoding="utf-8")
    settings = Settings(llm_model="fake", llm_effort="medium", anthropic_api_key=None,
                        llm_pricing=LLMPricing(0.1, 0.5, "test"))
    with pytest.raises(ValueError, match="different brief"):
        pipeline.run(tmp_path, settings=settings, queries_path=tmp_path / pipeline.QUERIES_FILE, search_clients={})


def test_out_of_credits_stops_the_run_instead_of_degrading_it():
    from sourcing_agent.extraction.enrichment import ExaContents
    from sourcing_agent.search.base import QuotaExhausted

    serper, _ = serper_with_pages({1: httpx.Response(400, json={"message": "Not enough credits", "statusCode": 400})})
    with pytest.raises(QuotaExhausted, match="Serper account is out of credits"):
        run_searches(plan_with([sq("serper")]), SearchSettings(), clients={"serper": serper})

    def exa_402(request):
        return httpx.Response(402, json={"error": "You have exceeded your credits limit."})
    contents = ExaContents("k", client=httpx.Client(transport=httpx.MockTransport(exa_402)))
    with pytest.raises(QuotaExhausted, match="Exa"):
        contents.fetch(["https://linkedin.com/in/a"])

    other, _ = serper_with_pages({1: httpx.Response(400, json={"message": "Query pattern not allowed"})})
    assert run_searches(plan_with([sq("serper")]), SearchSettings(), clients={"serper": other}).runs[0].errors


def test_a_rejected_api_key_stops_the_run_instead_of_failing_every_query():
    from sourcing_agent.search.base import InvalidCredentials

    serper, _ = serper_with_pages({1: httpx.Response(401, json={"message": "Unauthorized."})})
    with pytest.raises(InvalidCredentials, match="SERPER_API_KEY"):
        run_searches(plan_with([sq("serper")]), SearchSettings(), clients={"serper": serper})


def test_anthropic_auth_error_becomes_invalid_credentials():
    import anthropic

    from sourcing_agent.llm import AnthropicLLM
    from sourcing_agent.search.base import InvalidCredentials

    class RejectingClient:
        class messages:  # noqa: N801  (mirrors the SDK's client.messages.parse)
            @staticmethod
            def parse(**kwargs):
                raise anthropic.AuthenticationError(
                    "invalid x-api-key", body=None,
                    response=httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com")))

    llm = AnthropicLLM(Settings(llm_model="m", llm_effort="medium", anthropic_api_key="bad",
                                llm_pricing=LLMPricing(0.1, 0.5, "t")), client=RejectingClient())
    with pytest.raises(InvalidCredentials, match="ANTHROPIC_API_KEY"):
        llm.parse(stage="ranking", system="s", user="u", output_model=SearchTermsDraft)

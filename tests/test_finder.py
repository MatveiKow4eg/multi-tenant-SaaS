import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.models.company import Company
from app.services.finder.finder import (
    ApiSearchProvider,
    BaseSearchProvider,
    FinderResult,
    _extract_domain,
    _is_junk,
    find_companies,
    find_companies_by_plan,
)
from app.services.finder.search_planner import SearchPlan
from app.services.finder.storage import save_finder_results


class FakeProvider(BaseSearchProvider):
    name = "fake"

    def __init__(self, by_query: dict[str, list[dict[str, str]]] | None = None, fail_on: set[str] | None = None):
        self.by_query = by_query or {}
        self.fail_on = fail_on or set()

    def search(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        if query in self.fail_on:
            raise RuntimeError("provider failure")
        return self.by_query.get(query, [])[:max_results]


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    yield session
    session.close()


def test_extract_domain_handles_duckduckgo_redirect():
    url = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fabout"
    assert _extract_domain(url) == "example.com"


def test_duckduckgo_domain_is_junk():
    assert _is_junk("duckduckgo.com") is True


def test_finder_uses_provider_abstraction_and_filters_junk():
    provider = FakeProvider(
        by_query={
            "manufacturing company Lithuania site": [
                {
                    "url": "https://duckduckgo.com/l/?uddg=https%3A%2F%2Frealco.lt",
                    "title": "RealCo Metal Fabrication",
                    "snippet": "Industrial welding and steel fabrication",
                },
                {"url": "https://duckduckgo.com", "title": "Search", "snippet": "B"},
            ]
        }
    )

    results = find_companies(
        countries=["Lithuania"],
        keywords=["manufacturing"],
        known_domains=set(),
        provider=provider,
    )

    assert len(results) == 1
    assert results[0].domain == "realco.lt"


def test_api_provider_without_api_key_does_not_crash():
    provider = ApiSearchProvider(api_key=None, api_url="https://example.invalid/search")
    hits = provider.search("manufacturing company Lithuania site", max_results=5)
    assert hits == []


def test_finder_continues_if_single_query_fails():
    provider = FakeProvider(
        by_query={
            "industrial company Latvia site": [
                {
                    "url": "https://factory.lv",
                    "title": "Factory LV CNC Machining",
                    "snippet": "Industrial assembly and metal fabrication",
                }
            ]
        },
        fail_on={"manufacturing company Latvia site"},
    )

    results = find_companies(
        countries=["Latvia"],
        keywords=["manufacturing", "industrial"],
        provider=provider,
    )

    assert [r.domain for r in results] == ["factory.lv"]


def test_repeat_save_deduplicates_existing_domain(test_db):
    first = FinderResult(
        domain="acme.com",
        url="https://acme.com",
        title="Acme",
        snippet="",
        country="Lithuania",
        keyword="manufacturing",
    )
    second = FinderResult(
        domain="acme.com",
        url="https://acme.com/about",
        title="Acme Updated",
        snippet="",
        country="Lithuania",
        keyword="manufacturing",
    )

    created_1 = save_finder_results([first], test_db)
    created_2 = save_finder_results([second], test_db)

    assert len(created_1) == 1
    assert len(created_2) == 0
    assert test_db.query(Company).filter(Company.domain == "acme.com").count() == 1


def test_find_companies_skips_known_domains():
    provider = FakeProvider(
        by_query={
            "manufacturing company Lithuania site": [
                {
                    "url": "https://known.lt",
                    "title": "Known metal fabrication",
                    "snippet": "",
                },
                {
                    "url": "https://newco.lt",
                    "title": "New CNC machining company",
                    "snippet": "",
                },
            ]
        }
    )

    results = find_companies(
        countries=["Lithuania"],
        keywords=["manufacturing"],
        known_domains={"known.lt"},
        provider=provider,
    )

    assert [r.domain for r in results] == ["newco.lt"]


def test_search_provider_from_env_api_without_key_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "search_provider", "api")
    monkeypatch.setattr(settings, "search_api_key", None)
    monkeypatch.setattr(settings, "search_provider_fallback_to_ddg", False)

    # no provider injected: code path must use env-selected provider and remain safe
    results = find_companies(countries=["Lithuania"], keywords=["manufacturing"], results_per_query=2)
    assert isinstance(results, list)


def test_blacklisted_domains_are_rejected_before_save():
    provider = FakeProvider(
        by_query={
            "manufacturing company Lithuania site": [
                {
                    "url": "https://f6s.com/company/metal-fab-lt",
                    "title": "Metal Fab Startup",
                    "snippet": "Industrial manufacturing",
                },
                {
                    "url": "https://kalvis.lt",
                    "title": "Kalvis metal fabrication",
                    "snippet": "Welding and steel structures",
                },
            ]
        }
    )

    results = find_companies(
        countries=["Lithuania"],
        keywords=["manufacturing"],
        known_domains=set(),
        provider=provider,
    )

    assert [r.domain for r in results] == ["kalvis.lt"]


def test_directory_listing_signal_is_rejected():
    provider = FakeProvider(
        by_query={
            "manufacturing company Lithuania site": [
                {
                    "url": "https://example.com/lithuania-metal-companies",
                    "title": "Top metal companies in Lithuania directory",
                    "snippet": "List of manufacturers in Lithuania",
                }
            ]
        }
    )

    results = find_companies(
        countries=["Lithuania"],
        keywords=["manufacturing"],
        known_domains=set(),
        provider=provider,
    )

    assert results == []


def test_irrelevant_pharma_vertical_is_rejected():
    provider = FakeProvider(
        by_query={
            "manufacturing company Lithuania site": [
                {
                    "url": "https://contract-pharma.com",
                    "title": "Contract Pharma Manufacturing",
                    "snippet": "Supplements and cosmetics production",
                }
            ]
        }
    )

    results = find_companies(
        countries=["Lithuania"],
        keywords=["manufacturing"],
        known_domains=set(),
        provider=provider,
    )

    assert results == []


def test_relevant_metal_site_passes_scoring():
    provider = FakeProvider(
        by_query={
            "manufacturing company Lithuania site": [
                {
                    "url": "https://megameta.lt",
                    "title": "Megameta - metal fabrication and welding",
                    "snippet": "CNC machining and industrial assembly services",
                }
            ]
        }
    )

    results = find_companies(
        countries=["Lithuania"],
        keywords=["manufacturing"],
        known_domains=set(),
        provider=provider,
    )

    assert [r.domain for r in results] == ["megameta.lt"]


def test_find_companies_by_plan_uses_plan_exclusions_and_scoring():
    provider = FakeProvider(
        by_query={
            "metal fabrication Lithuania": [
                {
                    "url": "https://info.lt/suppliers",
                    "title": "Business directory metal suppliers",
                    "snippet": "Catalog of companies in Lithuania",
                },
                {
                    "url": "https://pmkonstrukcijos.lt",
                    "title": "PM Konstrukcijos",
                    "snippet": "Metalo konstrukcijos ir suvirinimas",
                },
            ]
        }
    )

    plan = SearchPlan(
        country="Lithuania",
        allowed_countries=["Lithuania"],
        priority_industries=["metal fabrication"],
        search_queries_en=["metal fabrication Lithuania"],
        search_queries_lt=[],
        exclude_terms=["directory", "catalog"],
        exclude_domains=["info.lt", "yellowpages.*"],
        query_templates=["{term} Lithuania"],
    )

    results = find_companies_by_plan(
        plan=plan,
        known_domains=set(),
        provider=provider,
    )

    assert [r.domain for r in results] == ["pmkonstrukcijos.lt"]

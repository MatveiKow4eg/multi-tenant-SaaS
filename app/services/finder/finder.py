"""
Finder service.

Production default is API-based search provider. DuckDuckGo HTML scraping is optional
fallback-only because it is unstable under anti-bot protections.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.core.config import settings

logger = logging.getLogger("finder")

INDUSTRY_KEYWORDS: list[str] = [
    "manufacturing",
    "industrial",
    "factory",
    "plant",
    "fabrication",
    "welding",
    "metal",
    "production",
    "subcontract manufacturing",
]

# Clearly non-target domains for company discovery.
JUNK_DOMAINS: set[str] = {
    "google.com",
    "bing.com",
    "yahoo.com",
    "duckduckgo.com",
    "baidu.com",
    "yandex.com",
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
    "tiktok.com",
    "reddit.com",
    "wikipedia.org",
    "bloomberg.com",
    "reuters.com",
    "bbc.com",
    "cnn.com",
    "indeed.com",
    "glassdoor.com",
    "jobs.com",
    "monster.com",
    "crunchbase.com",
    "pitchbook.com",
    "github.com",
    "stackoverflow.com",
    "f6s.com",
    "ezilon.com",
    "info.lt",
    "paslaugos.lt",
    "statyba.lt",
    "infocloud.lt",
    "cphi-online.com",
    "biopharmiq.com",
    "machinesequipments.com",
    "contract-pharma.com",
    "ritamumpharma.com",
    "zoominfo.com",
    "clutch.co",
    "lusha.com",
    "europages.com",
    "kompass.com",
    "yellowpages.com",
    "imones.lt",
    "rekvizitai.lt",
    "118.lt",
    "visalietuva.lt",
}

JUNK_DOMAIN_PREFIXES: list[str] = ["google.", "yahoo."]
JUNK_DOMAIN_WILDCARD_ROOTS: set[str] = {
    "yellowpages",
    "europages",
}

NEGATIVE_LISTING_SIGNALS: set[str] = {
    "list of",
    "top",
    "ranking",
    "directory",
    "business directory",
    "catalog",
    "catalogue",
    "prices",
    "kainos",
    "supplier list",
    "manufacturers in",
    "companies in",
}

NEGATIVE_IRRELEVANT_VERTICAL_SIGNALS: set[str] = {
    "production house",
    "film",
    "video production",
    "nursery",
    "flowers",
    "pharma",
    "cosmetics",
    "supplements",
}

POSITIVE_SIGNALS: set[str] = {
    "metal",
    "steel",
    "fabrication",
    "welding",
    "cnc",
    "machining",
    "machine shop",
    "subcontract manufacturing",
    "industrial assembly",
    "sheet metal",
    "konstrukcijos",
    "metalo",
    "suvirinimas",
    "apdirbimas",
    "gamyba",
    "pramone",
    "plieno konstrukcijos",
}

STRONG_POSITIVE_SIGNALS: set[str] = {
    "metal fabrication",
    "steel structures",
    "cnc machining",
    "industrial equipment",
    "subcontract manufacturing",
    "machine shop",
    "sheet metal",
    "metalo apdirbimas",
    "metalo konstrukcijos",
    "suvirinimas",
    "plieno konstrukcijos",
    "pramones iranga",
}

MIN_RELEVANCE_SCORE = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


@dataclass
class FinderResult:
    domain: str
    url: str
    title: str
    snippet: str
    country: str
    keyword: str


@dataclass
class RelevanceDecision:
    accepted: bool
    score: int
    reason: str


class BaseSearchProvider(ABC):
    name: str

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        """Return normalized hits as dicts with keys: url, title, snippet."""


class ApiSearchProvider(BaseSearchProvider):
    """API-first provider for production usage."""

    name = "api"

    def __init__(self, api_key: str | None, api_url: str):
        self.api_key = api_key
        self.api_url = api_url

    def search(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        if not self.api_key:
            logger.warning("Finder: SEARCH_API_KEY is empty, API provider cannot run")
            return []

        payload = {"q": query, "num": max_results}
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                resp = client.post(self.api_url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Finder: API search request failed for query '%s': %s", query, exc)
            return []

        raw_items = []
        if isinstance(data, dict):
            if isinstance(data.get("organic"), list):
                raw_items = data["organic"]
            elif isinstance(data.get("results"), list):
                raw_items = data["results"]
            elif isinstance(data.get("items"), list):
                raw_items = data["items"]

        normalized: list[dict[str, str]] = []
        for item in raw_items[:max_results]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("link") or item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or item.get("description") or "").strip()
            if not url:
                continue
            normalized.append({"url": url, "title": title, "snippet": snippet})

        return normalized


class DuckDuckGoHtmlProvider(BaseSearchProvider):
    """Legacy HTML scraping provider. Fallback-only due to anti-bot limits."""

    name = "duckduckgo_html"

    def search(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        try:
            resp = httpx.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=HEADERS,
                timeout=20,
                follow_redirects=True,
            )
            resp.raise_for_status()
            links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)<', resp.text)
            snippets = re.findall(r'class="result__snippet"[^>]*>([^<]+)<', resp.text)
            for i, (url, title) in enumerate(links[:max_results]):
                snippet = snippets[i] if i < len(snippets) else ""
                results.append({"url": url, "title": title.strip(), "snippet": snippet.strip()})
        except Exception as exc:
            logger.warning("Finder: DDG HTML search failed for query '%s': %s", query, exc)
        return results


def _get_search_provider() -> BaseSearchProvider:
    provider_name = settings.search_provider.lower().strip()

    if provider_name == "api":
        provider = ApiSearchProvider(
            api_key=settings.search_api_key,
            api_url=settings.search_api_url,
        )
        if not settings.search_api_key and settings.search_provider_fallback_to_ddg:
            logger.warning(
                "Finder: SEARCH_PROVIDER=api but SEARCH_API_KEY is missing; "
                "fallback to DuckDuckGoHtmlProvider is enabled"
            )
            return DuckDuckGoHtmlProvider()
        return provider

    if provider_name in {"ddg", "duckduckgo", "duckduckgo_html"}:
        logger.warning("Finder: using DuckDuckGoHtmlProvider explicitly (legacy/fallback mode)")
        return DuckDuckGoHtmlProvider()

    logger.warning("Finder: unknown SEARCH_PROVIDER='%s', defaulting to API provider", provider_name)
    return ApiSearchProvider(api_key=settings.search_api_key, api_url=settings.search_api_url)


def _extract_domain(url: str) -> str | None:
    """
    Extract and normalize domain from URL.
    Supports DuckDuckGo redirect links like:
    https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com
    """
    try:
        parsed = urlparse(url)

        # DuckDuckGo redirect -> extract real target from uddg=
        if "duckduckgo.com" in (parsed.netloc or "") and parsed.path.startswith("/l/"):
            qs = parse_qs(parsed.query)
            target = qs.get("uddg", [None])[0]
            if target:
                url = unquote(target)
                parsed = urlparse(url)

        host = (parsed.netloc or "").lower().strip()

        if not host:
            return None

        if host.startswith("www."):
            host = host[4:]

        if ":" in host:
            host = host.split(":")[0]

        if not host or len(host) < 3 or "." not in host:
            return None

        return host
    except Exception:
        return None


def _is_domain_blacklisted(domain: str, extra_domains: set[str] | None = None) -> bool:
    if any(domain == d or domain.endswith("." + d) for d in JUNK_DOMAINS):
        return True

    if extra_domains and any(domain == d or domain.endswith("." + d) for d in extra_domains):
        return True

    if any(domain.startswith(prefix) for prefix in JUNK_DOMAIN_PREFIXES):
        return True

    labels = domain.split(".")
    if any(root in labels for root in JUNK_DOMAIN_WILDCARD_ROOTS):
        return True

    return False


def _is_junk(domain: str) -> bool:
    return _is_domain_blacklisted(domain)


def _normalize_text_for_scoring(domain: str, url: str, title: str, snippet: str) -> str:
    return " ".join([domain or "", url or "", title or "", snippet or ""]).lower()


def _evaluate_relevance(
    *,
    domain: str,
    url: str,
    title: str,
    snippet: str,
    extra_blacklisted_domains: set[str] | None = None,
    extra_negative_terms: set[str] | None = None,
) -> RelevanceDecision:
    if _is_domain_blacklisted(domain, extra_domains=extra_blacklisted_domains):
        return RelevanceDecision(accepted=False, score=-999, reason="skipped_blacklisted_domain")

    combined = _normalize_text_for_scoring(domain, url, title, snippet)

    listing_hits = [s for s in NEGATIVE_LISTING_SIGNALS if s in combined]
    irrelevant_hits = [s for s in NEGATIVE_IRRELEVANT_VERTICAL_SIGNALS if s in combined]
    positive_hits = [s for s in POSITIVE_SIGNALS if s in combined]
    strong_positive_hits = [s for s in STRONG_POSITIVE_SIGNALS if s in combined]
    extra_negative_hits = [s for s in (extra_negative_terms or set()) if s in combined]

    if irrelevant_hits:
        return RelevanceDecision(accepted=False, score=-5, reason="skipped_irrelevant_vertical")

    score = 0
    score += 2 * len(positive_hits)
    score += 3 * len(strong_positive_hits)
    score -= 4 * len(listing_hits)
    score -= 4 * len(extra_negative_hits)

    if score < MIN_RELEVANCE_SCORE:
        if listing_hits:
            return RelevanceDecision(accepted=False, score=score, reason="skipped_negative_title_signal")
        return RelevanceDecision(accepted=False, score=score, reason="skipped_low_relevance_score")

    return RelevanceDecision(accepted=True, score=score, reason=f"accepted_relevance_score={score}")


def find_companies(
    countries: list[str],
    keywords: list[str] | None = None,
    results_per_query: int = 10,
    known_domains: set[str] | None = None,
    provider: BaseSearchProvider | None = None,
) -> list[FinderResult]:
    """
    Search for companies matching keywords in countries.

    External contract remains unchanged. Additional provider argument is optional
    and is used mostly for tests/injection.
    """
    if keywords is None:
        keywords = INDUSTRY_KEYWORDS

    active_provider = provider or _get_search_provider()
    logger.info("Finder: selected search provider=%s", active_provider.name)

    seen: set[str] = set(known_domains or [])
    output: list[FinderResult] = []

    query_count = 0
    raw_results_total = 0
    junk_filtered = 0
    normalized_domains = 0
    invalid_or_empty = 0
    duplicates_skipped = 0
    reason_counts: Counter[str] = Counter()

    for country in countries:
        for kw in keywords:
            query_count += 1
            query = f"{kw} company {country} site"
            try:
                hits = active_provider.search(query, max_results=results_per_query)
            except Exception as exc:
                logger.warning("Finder: provider failure for query '%s': %s", query, exc)
                continue

            raw_results_total += len(hits)
            for hit in hits:
                url = hit.get("url", "")
                title = hit.get("title", "")
                snippet = hit.get("snippet", "")

                domain = _extract_domain(url)
                if not domain:
                    invalid_or_empty += 1
                    continue

                normalized_domains += 1

                if domain in seen:
                    duplicates_skipped += 1
                    continue

                decision = _evaluate_relevance(
                    domain=domain,
                    url=url,
                    title=title,
                    snippet=snippet,
                )
                if not decision.accepted:
                    reason_counts[decision.reason] += 1
                    if decision.reason == "skipped_blacklisted_domain":
                        junk_filtered += 1
                    logger.info(
                        "Finder: %s domain=%s query='%s' score=%d",
                        decision.reason,
                        domain,
                        query,
                        decision.score,
                    )
                    continue

                seen.add(domain)
                output.append(
                    FinderResult(
                        domain=domain,
                        url=url,
                        title=title.strip(),
                        snippet=snippet.strip(),
                        country=country,
                        keyword=kw,
                    )
                )
                logger.info("Finder: %s domain=%s query='%s'", decision.reason, domain, query)

    logger.info(
        "Finder: queries=%d raw_results=%d normalized=%d junk_filtered=%d "
        "invalid=%d duplicates=%d accepted=%d skipped_blacklisted_domain=%d "
        "skipped_negative_title_signal=%d skipped_irrelevant_vertical=%d "
        "skipped_low_relevance_score=%d",
        query_count,
        raw_results_total,
        normalized_domains,
        junk_filtered,
        invalid_or_empty,
        duplicates_skipped,
        len(output),
        reason_counts.get("skipped_blacklisted_domain", 0),
        reason_counts.get("skipped_negative_title_signal", 0),
        reason_counts.get("skipped_irrelevant_vertical", 0),
        reason_counts.get("skipped_low_relevance_score", 0),
    )
    return output


def find_companies_by_plan(
    plan: "SearchPlan",  # noqa: F821 – imported lazily to avoid circular dep
    results_per_query: int = 10,
    known_domains: set[str] | None = None,
    provider: BaseSearchProvider | None = None,
) -> list[FinderResult]:
    """
    Execute all search queries from a SearchPlan and return deduplicated FinderResult list.

    Applies exclude_terms and exclude_domains from the plan on top of the baseline
    JUNK_DOMAINS filter.
    """
    from app.services.finder.search_planner import SearchPlan  # local import, avoids circular

    active_provider = provider or _get_search_provider()
    logger.info(
        "Finder[plan]: provider=%s country=%s en_queries=%d lt_queries=%d",
        active_provider.name,
        plan.country,
        len(plan.search_queries_en),
        len(plan.search_queries_lt),
    )

    # Build exclude sets from plan
    plan_exclude_domains: set[str] = set()
    for d in plan.exclude_domains:
        # support wildcard prefix like "yellowpages.*" -> strip ".*"
        clean = d.rstrip(".*").lower()
        if clean:
            plan_exclude_domains.add(clean)

    plan_exclude_terms: set[str] = {t.lower() for t in plan.exclude_terms}

    seen: set[str] = set(known_domains or [])
    output: list[FinderResult] = []

    query_count = 0
    raw_results_total = 0
    junk_filtered = 0
    plan_junk_filtered = 0
    duplicates_skipped = 0
    reason_counts: Counter[str] = Counter()

    queries_en = plan.search_queries_en
    queries_lt = plan.search_queries_lt
    all_queries_deduped: list[str] = []
    seen_q: set[str] = set()
    for q in queries_en + queries_lt:
        q_n = q.strip()
        if q_n and q_n not in seen_q:
            seen_q.add(q_n)
            all_queries_deduped.append(q_n)

    for query in all_queries_deduped:
        query_count += 1
        try:
            hits = active_provider.search(query, max_results=results_per_query)
        except Exception as exc:
            logger.warning("Finder[plan]: provider failure for query '%s': %s", query, exc)
            continue

        raw_results_total += len(hits)
        for hit in hits:
            url = hit.get("url", "")
            title = hit.get("title", "")
            snippet = hit.get("snippet", "")

            domain = _extract_domain(url)
            if not domain:
                continue

            if domain in seen:
                duplicates_skipped += 1
                continue

            decision = _evaluate_relevance(
                domain=domain,
                url=url,
                title=title,
                snippet=snippet,
                extra_blacklisted_domains=plan_exclude_domains,
                extra_negative_terms=plan_exclude_terms,
            )
            if not decision.accepted:
                reason_counts[decision.reason] += 1
                if decision.reason == "skipped_blacklisted_domain":
                    junk_filtered += 1
                    if any(domain == d or domain.endswith("." + d) for d in plan_exclude_domains):
                        plan_junk_filtered += 1
                logger.info(
                    "Finder[plan]: %s domain=%s query='%s' score=%d",
                    decision.reason,
                    domain,
                    query,
                    decision.score,
                )
                continue

            seen.add(domain)
            output.append(
                FinderResult(
                    domain=domain,
                    url=url,
                    title=title.strip(),
                    snippet=snippet.strip(),
                    country=plan.country,
                    keyword=query,
                )
            )
            logger.info("Finder[plan]: %s domain=%s query='%s'", decision.reason, domain, query)

    logger.info(
        "Finder[plan]: queries=%d raw=%d junk=%d plan_junk=%d dupes=%d accepted=%d "
        "skipped_blacklisted_domain=%d skipped_negative_title_signal=%d "
        "skipped_irrelevant_vertical=%d skipped_low_relevance_score=%d",
        query_count,
        raw_results_total,
        junk_filtered,
        plan_junk_filtered,
        duplicates_skipped,
        len(output),
        reason_counts.get("skipped_blacklisted_domain", 0),
        reason_counts.get("skipped_negative_title_signal", 0),
        reason_counts.get("skipped_irrelevant_vertical", 0),
        reason_counts.get("skipped_low_relevance_score", 0),
    )
    return output

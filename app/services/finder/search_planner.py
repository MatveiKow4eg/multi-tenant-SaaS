"""
AI Search Planner — generates a structured search plan from a free-text user intent.

Usage:
    plan = generate_search_plan("Ищи литовские заводы в сферах сварки и CNC")
    find_companies_by_plan(plan, results_per_query=20)

Guardrail: planner only runs for Lithuania. Other countries raise ValueError.
"""
from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache

from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger("search_planner")

ALLOWED_COUNTRIES = {"lithuania"}

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class SearchPlan(BaseModel):
    country: str
    allowed_countries: list[str]
    priority_industries: list[str]
    search_queries_en: list[str]
    search_queries_lt: list[str]
    exclude_terms: list[str]
    exclude_domains: list[str]
    query_templates: list[str]

    def all_queries(self) -> list[str]:
        """Deduplicated list of all search queries (EN + LT)."""
        seen: set[str] = set()
        result: list[str] = []
        for q in self.search_queries_en + self.search_queries_lt:
            q_norm = q.strip()
            if q_norm and q_norm not in seen:
                seen.add(q_norm)
                result.append(q_norm)
        return result


# ---------------------------------------------------------------------------
# Fallback plan
# ---------------------------------------------------------------------------

_FALLBACK_PLAN = SearchPlan(
    country="Lithuania",
    allowed_countries=["Lithuania"],
    priority_industries=[
        "metal fabrication",
        "welding",
        "subcontract manufacturing",
        "industrial equipment manufacturing",
        "CNC machining",
        "industrial assembly",
        "steel structures",
    ],
    search_queries_en=[
        "metal fabrication company Lithuania",
        "steel structures manufacturer Lithuania",
        "welding company Lithuania",
        "contract manufacturing Lithuania",
        "CNC machining Lithuania",
        "industrial assembly Lithuania",
        "subcontract manufacturing Lithuania",
        "industrial equipment manufacturer Lithuania",
        "sheet metal fabrication Lithuania",
        "metal construction company Lithuania",
    ],
    search_queries_lt=[
        "metalo apdirbimas Lietuva",
        "metalo konstrukcijos Lietuva",
        "suvirinimas įmonė Lietuva",
        "gamybos įmonė Lietuva",
        "CNC apdirbimas Lietuva",
        "pramonės įmonė Lietuva",
        "subrangos gamyba Lietuva",
        "inžinerinė gamyba Lietuva",
        "plieno konstrukcijos Lietuva",
        "metalo gaminiai Lietuva",
    ],
    exclude_terms=[
        "film",
        "video production",
        "plants",
        "nursery",
        "flowers",
        "directory",
        "listing",
        "database",
        "lead generation",
        "ratings",
        "ranking",
        "catalog",
        "catalogue",
    ],
    exclude_domains=[
        "zoominfo.com",
        "clutch.co",
        "lusha.com",
        "europages.com",
        "kompass.com",
        "yellowpages.com",
        "goldenpages.lt",
        "rekvizitai.lt",
    ],
    query_templates=[
        "{term} Lithuania",
        "{term} company Lithuania",
        "{term} manufacturer Lithuania",
        "{term} supplier Lithuania",
        "{term} Lietuva",
        "{term} gamyba Lietuva",
    ],
)


# ---------------------------------------------------------------------------
# OpenAI planner
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert B2B lead-generation assistant for a recruitment and outsourcing company
based in Lithuania. Your task is to generate a structured JSON search plan for finding
target companies in Lithuania.

Rules:
- Output ONLY valid JSON, no markdown, no explanation.
- The plan must target only Lithuania (country: "Lithuania").
- Generate both English and Lithuanian search queries.
- Include at least 8 English queries and 8 Lithuanian queries.
- Lithuanian queries should use real Lithuanian industry vocabulary.
- Avoid query templates that would match directories, catalogs, rankings, film/media companies,
  nurseries, or lead-gen aggregators.
- Include exclude_terms covering clearly non-target content.
- The JSON must match this schema exactly:
  {
    "country": "Lithuania",
    "allowed_countries": ["Lithuania"],
    "priority_industries": [...],
    "search_queries_en": [...],
    "search_queries_lt": [...],
    "exclude_terms": [...],
    "exclude_domains": [...],
    "query_templates": [...]
  }
"""


def _intent_cache_key(intent: str, country: str) -> str:
    raw = f"{country.lower()}::{intent.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# Simple in-process LRU cache keyed by intent hash (avoids burning OpenAI tokens).
@lru_cache(maxsize=64)
def _cached_openai_plan(cache_key: str, intent: str, country: str) -> SearchPlan:  # noqa: ARG001
    return _call_openai(intent, country)


def _call_openai(intent: str, country: str) -> SearchPlan:
    try:
        from openai import OpenAI  # type: ignore

        client = OpenAI(api_key=settings.openai_api_key)
        user_msg = (
            f"Country: {country}\n"
            f"User intent: {intent}\n\n"
            "Generate the search plan JSON."
        )
        response = client.responses.create(
            model=settings.openai_mini_model,
            instructions=_SYSTEM_PROMPT,
            input=user_msg,
            temperature=0.3,
            max_output_tokens=1500,
            text={"format": {"type": "json_object"}},
        )
        raw_json = (response.output_text or "").strip()
        if not raw_json:
            # Compatibility fallback for SDK variants where output_text may be empty.
            parts: list[str] = []
            for item in getattr(response, "output", []) or []:
                for content in getattr(item, "content", []) or []:
                    if getattr(content, "type", "") == "output_text":
                        text_value = getattr(content, "text", "")
                        if text_value:
                            parts.append(text_value)
            raw_json = "\n".join(parts).strip()

        if not raw_json:
            raise ValueError("OpenAI returned empty planner response")

        data = json.loads(raw_json)
        plan = SearchPlan(**data)
        logger.info(
            "SearchPlanner: OpenAI plan generated — en_queries=%d lt_queries=%d industries=%d",
            len(plan.search_queries_en),
            len(plan.search_queries_lt),
            len(plan.priority_industries),
        )
        return plan
    except Exception as exc:
        logger.warning("SearchPlanner: OpenAI call failed (%s), falling back to default plan", exc)
        return _FALLBACK_PLAN


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_search_plan(user_intent: str, country: str = "Lithuania") -> SearchPlan:
    """
    Generate a structured search plan from a free-text user intent.

    Raises ValueError if the country is not Lithuania (current guardrail).
    Falls back to the default Lithuania plan if OpenAI is unavailable.
    """
    if country.strip().lower() not in ALLOWED_COUNTRIES:
        raise ValueError(
            f"SearchPlanner is currently restricted to Lithuania. Got: '{country}'. "
            "Support for other countries will be added later."
        )

    logger.info("SearchPlanner: generating plan for country='%s' intent_len=%d", country, len(user_intent))

    if not settings.openai_api_key:
        logger.warning("SearchPlanner: OPENAI_API_KEY not set, using fallback plan")
        return _FALLBACK_PLAN

    cache_key = _intent_cache_key(user_intent, country)
    return _cached_openai_plan(cache_key, user_intent, country)

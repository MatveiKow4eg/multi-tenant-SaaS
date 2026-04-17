from __future__ import annotations

import json

from openai import OpenAI

from app.core.config import settings
from app.services.researcher.site_researcher import ResearchResult


QUALIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "is_relevant": {"type": "boolean"},
        "industry": {"type": "string"},
        "country": {"type": "string"},
        "recommended_language": {"type": "string"},
        "signals": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": [
        "is_relevant",
        "industry",
        "country",
        "recommended_language",
        "signals",
        "score",
        "reason",
    ],
    "additionalProperties": False,
}


def _fallback_qualification(research: ResearchResult) -> dict:
    low = research.text_summary.lower()
    is_relevant = any(k in low for k in ["manufactur", "factory", "industrial", "production", "fabrication"])
    score = 70 if is_relevant else 25
    if research.has_careers_page:
        score += 10
    score = min(score, 100)
    return {
        "is_relevant": is_relevant,
        "industry": "manufacturing" if is_relevant else "unknown",
        "country": "unknown",
        "recommended_language": "en",
        "signals": [
            "fallback_heuristics",
            "careers_page_found" if research.has_careers_page else "no_careers_page",
        ],
        "score": score,
        "reason": "Fallback heuristic qualification used because OpenAI is not configured or failed.",
    }


def qualify_company(research: ResearchResult) -> dict:
    if not settings.openai_api_key:
        return _fallback_qualification(research)

    client = OpenAI(api_key=settings.openai_api_key)
    prompt = (
        "You are a B2B manufacturing lead qualification assistant. "
        "Analyze the website text and return strict JSON according to schema. "
        "Score 0..100 where 100 is highly relevant manufacturing company with strong hiring/operations signal.\n\n"
        f"Domain: {research.domain}\n"
        f"Has careers page: {research.has_careers_page}\n"
        f"Detected language hints: {', '.join(research.languages_found) if research.languages_found else 'none'}\n"
        f"Website text:\n{research.text_summary[:12000]}"
    )

    try:
        resp = client.responses.create(
            model=settings.openai_main_model,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "company_qualification",
                    "schema": QUALIFIER_SCHEMA,
                    "strict": True,
                }
            },
        )
        raw = getattr(resp, "output_text", "")
        return json.loads(raw)
    except Exception:
        return _fallback_qualification(research)

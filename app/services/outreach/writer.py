from __future__ import annotations

import hashlib
import json
import logging

from openai import OpenAI

from app.core.config import settings
from app.services.outreach.policy import language_for_country
from app.services.outreach.templates_lithuania import (
    OutreachTemplate,
    lithuania_first_touch_templates,
    lithuania_followup_templates,
)

logger = logging.getLogger("outreach.writer")

WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body_step_1": {"type": "string"},
        "body_followup_1": {"type": "string"},
        "body_followup_2": {"type": "string"},
    },
    "required": ["subject", "body_step_1", "body_followup_1", "body_followup_2"],
    "additionalProperties": False,
}


def _render_template(template: OutreachTemplate, *, company_name: str, industry: str) -> tuple[str, str]:
    return (
        template.subject.format(company_name=company_name, industry=industry),
        template.body.format(company_name=company_name, industry=industry),
    )


def _stable_index(seed: str, size: int) -> int:
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return int(digest, 16) % max(size, 1)


def _select_lithuania_templates(
    *,
    company_seed: str,
) -> tuple[OutreachTemplate, OutreachTemplate, OutreachTemplate]:
    first_idx = _stable_index(f"{company_seed}:first", len(lithuania_first_touch_templates))
    follow_idx = _stable_index(f"{company_seed}:follow", len(lithuania_followup_templates))

    first = lithuania_first_touch_templates[first_idx]
    followup_1 = lithuania_followup_templates[follow_idx]
    followup_2 = lithuania_followup_templates[(follow_idx + 1) % len(lithuania_followup_templates)]
    return first, followup_1, followup_2


def _fallback_copy(
    *,
    company_name: str,
    industry: str,
    country: str,
    company_seed: str,
) -> dict:
    if language_for_country(country) == "lt":
        first, followup_1, followup_2 = _select_lithuania_templates(company_seed=company_seed)
        subject, body_1 = _render_template(first, company_name=company_name, industry=industry)
        _, body_fu_1 = _render_template(followup_1, company_name=company_name, industry=industry)
        _, body_fu_2 = _render_template(followup_2, company_name=company_name, industry=industry)
        return {
            "subject": subject,
            "body_step_1": body_1,
            "body_followup_1": body_fu_1,
            "body_followup_2": body_fu_2,
        }

    return {
        "subject": f"Partnership inquiry for {company_name}",
        "body_step_1": (
            f"Hello,\n\n"
            f"I noticed {company_name} operates in {industry}. "
            "Would you be open to a short intro call next week?"
        ),
        "body_followup_1": "Following up on my previous message. Would a short intro call be useful?",
        "body_followup_2": "Final follow-up from my side. If this is not relevant now, let me know.",
    }


def generate_outreach_sequence(
    *,
    company_name: str,
    industry: str,
    country: str,
    recipient_role: str,
    language: str,
    brief: str,
    max_length: int = 900,
) -> dict:
    target_language = language_for_country(country)
    if target_language == "lt":
        language = "lt"

    company_seed = f"{company_name}:{industry}:{country}".lower()
    fallback = _fallback_copy(
        company_name=company_name,
        industry=industry,
        country=country,
        company_seed=company_seed,
    )

    first, followup_1, followup_2 = _select_lithuania_templates(company_seed=company_seed)
    logger.info(
        "Outreach Writer: selected Lithuania templates first=%s followup1=%s followup2=%s",
        first.template_id,
        followup_1.template_id,
        followup_2.template_id,
    )

    if not settings.openai_api_key:
        logger.info("Outreach Writer: OpenAI unavailable, using template fallback in language=%s", language)
        return fallback

    first_subject, first_body = _render_template(first, company_name=company_name, industry=industry)
    _, fu1_body = _render_template(followup_1, company_name=company_name, industry=industry)
    _, fu2_body = _render_template(followup_2, company_name=company_name, industry=industry)

    client = OpenAI(api_key=settings.openai_api_key)
    prompt = (
        "Adapt outreach templates for this specific manufacturing company. "
        "Do NOT write from scratch. Keep structure close to templates, just personalize. "
        "No fake claims, no invented facts, no aggressive sales tone.\n\n"
        f"Language code: {language}\n"
        f"Country: {country}\n"
        f"Company: {company_name}\n"
        f"Industry: {industry}\n"
        f"Recipient role: {recipient_role}\n"
        f"Max length per email: {max_length} chars\n"
        f"Brief and signals: {brief[:3000]}\n\n"
        "Base templates to adapt:\n"
        f"Subject template: {first_subject}\n"
        f"First email template:\n{first_body}\n\n"
        f"Follow-up #1 template:\n{fu1_body}\n\n"
        f"Follow-up #2 template:\n{fu2_body}\n"
    )

    try:
        resp = client.responses.create(
            model=settings.openai_main_model,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "outreach_sequence",
                    "schema": WRITER_SCHEMA,
                    "strict": True,
                }
            },
        )
        parsed = json.loads(getattr(resp, "output_text", "{}"))
        logger.info("Outreach Writer: generated outreach in language=%s", language)
        return parsed
    except Exception:
        logger.warning("Outreach Writer: OpenAI failed, using template fallback in language=%s", language)
        return fallback

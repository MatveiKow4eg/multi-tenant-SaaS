from __future__ import annotations

import json

from openai import OpenAI

from app.core.config import settings

ALLOWED_LABELS = [
    "interested",
    "ask_for_details",
    "send_rates",
    "not_now",
    "not_interested",
    "wrong_contact",
    "bounced",
    "auto_reply",
    "out_of_office",
    "forwarded_internal",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ALLOWED_LABELS},
        "summary": {"type": "string"},
        "needs_human": {"type": "boolean"},
        "next_action": {"type": "string"},
    },
    "required": ["label", "summary", "needs_human", "next_action"],
    "additionalProperties": False,
}


def _fallback_classify(subject: str, body: str) -> dict:
    low = f"{subject} {body}".lower()
    if any(
        k in low
        for k in [
            "mailer-daemon",
            "delivery status notification",
            "delivery has failed",
            "delivery failed",
            "undeliverable",
            "recipient address rejected",
            "user unknown",
            "unknown user",
            "5.1.1",
            "550 5.1.1",
        ]
    ):
        return {
            "label": "bounced",
            "summary": "Delivery failed (bounce) detected for outbound email.",
            "needs_human": False,
            "next_action": "stop_campaign_and_mark_bounce",
        }
    if any(k in low for k in ["out of office", "automatic reply", "auto-reply"]):
        return {
            "label": "out_of_office",
            "summary": "Automatic out-of-office or auto-reply detected.",
            "needs_human": False,
            "next_action": "wait",
        }
    if any(k in low for k in ["not interested", "do not contact", "stop emailing", "unsubscribe"]):
        return {
            "label": "not_interested",
            "summary": "Recipient rejected outreach or asked to stop.",
            "needs_human": False,
            "next_action": "blacklist_and_stop",
        }
    if any(k in low for k in ["price", "rates", "quote", "cost"]):
        return {
            "label": "send_rates",
            "summary": "Contact asks for rates or pricing details.",
            "needs_human": True,
            "next_action": "notify_user",
        }
    if any(k in low for k in ["interested", "let's talk", "call", "meeting"]):
        return {
            "label": "interested",
            "summary": "Contact shows positive intent for discussion.",
            "needs_human": True,
            "next_action": "notify_user",
        }
    return {
        "label": "ask_for_details",
        "summary": "Reply requires manual review and likely details.",
        "needs_human": True,
        "next_action": "notify_user",
    }


def classify_reply(subject: str, body: str) -> dict:
    if not settings.openai_api_key:
        return _fallback_classify(subject, body)

    client = OpenAI(api_key=settings.openai_api_key)
    prompt = (
        "Classify the inbound B2B email reply. Return strict JSON schema. "
        "Choose one label from allowed taxonomy and decide if human is needed.\n\n"
        f"Subject: {subject}\n"
        f"Body:\n{body[:12000]}"
    )
    try:
        resp = client.responses.create(
            model=settings.openai_main_model,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "reply_classification",
                    "schema": SCHEMA,
                    "strict": True,
                }
            },
        )
        return json.loads(getattr(resp, "output_text", "{}"))
    except Exception:
        return _fallback_classify(subject, body)

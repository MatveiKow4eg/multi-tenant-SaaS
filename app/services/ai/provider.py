"""
Multi-provider AI layer with automatic fallback.

Strategy:
  1. Try main model (e.g. gpt-5.4)
  2. On failure → try mini model (e.g. gpt-5.4-mini)
  3. On failure → raise AIProviderError so caller can use heuristic fallback

Usage:
    from app.services.ai.provider import ai_call

    result_text = ai_call(
        prompt="...",
        json_schema=MY_SCHEMA,  # optional structured output
    )
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ai.provider")


class AIProviderError(Exception):
    """Raised when all AI providers have failed."""


def ai_call(
    prompt: str,
    *,
    json_schema: dict | None = None,
    schema_name: str = "response",
    max_tokens: int = 2000,
) -> str:
    """
    Call AI with automatic fallback: main_model → mini_model → raise AIProviderError.

    Returns the raw text response (JSON string if json_schema provided).
    """
    from app.core.config import settings

    if not settings.openai_api_key:
        raise AIProviderError("No OpenAI API key configured")

    from openai import OpenAI, APIError, RateLimitError, APITimeoutError

    client = OpenAI(api_key=settings.openai_api_key)

    models = [settings.openai_main_model, settings.openai_mini_model]
    last_exc: Exception | None = None

    for model in models:
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "input": prompt,
            }
            if json_schema is not None:
                kwargs["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": json_schema,
                        "strict": True,
                    }
                }

            resp = client.responses.create(**kwargs)
            text = getattr(resp, "output_text", "") or ""
            logger.info("ai_call: success with model=%s schema=%s", model, schema_name)
            return text

        except (RateLimitError, APITimeoutError, APIError) as exc:
            logger.warning("ai_call: model=%s failed: %s — trying next", model, exc)
            last_exc = exc
        except Exception as exc:
            logger.warning("ai_call: model=%s unexpected error: %s — trying next", model, exc)
            last_exc = exc

    raise AIProviderError(f"All AI providers failed. Last error: {last_exc}")

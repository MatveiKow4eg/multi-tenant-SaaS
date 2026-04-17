from __future__ import annotations

import hmac
import secrets
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import Response

from app.core.config import settings


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def issue_csrf_cookie_if_missing(request: Request, response: Response) -> None:
    cookie_name = settings.auth_csrf_cookie_name
    existing = request.cookies.get(cookie_name)
    if existing:
        return
    response.set_cookie(
        key=cookie_name,
        value=_new_token(),
        httponly=False,
        secure=settings.auth_csrf_cookie_secure,
        samesite="lax",
        path="/",
    )


def should_enforce_csrf(request: Request) -> bool:
    # CSRF is relevant when authentication is cookie-based.
    return request.cookies.get(settings.auth_session_cookie_name) is not None


async def is_valid_csrf(request: Request) -> bool:
    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name)
    if not cookie_token:
        return False

    header_token = request.headers.get("X-CSRF-Token")

    form_token = None
    if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raw_body = await request.body()
        parsed = parse_qs(raw_body.decode("utf-8", errors="ignore"), keep_blank_values=True)
        values = parsed.get("_csrf_token")
        if values:
            form_token = values[0]

    submitted = header_token or form_token
    if not submitted:
        return False
    return hmac.compare_digest(str(submitted), str(cookie_token))

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.services.auth.session_manager import resolve_active_session


# Sentinel tenant id that never matches real tenant rows.
_NO_TENANT_ID = -1


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        token = authorization[len(prefix):].strip()
        return token or None
    return None


def get_tenant_id(
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-Id"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    request: Request = None,
    db: Session = Depends(get_db),
) -> int | None:
    is_ui_request = bool(request is not None and (request.url.path == "/ui" or request.url.path.startswith("/ui/")))

    if is_ui_request:
        bearer_token = _extract_bearer_token(authorization)
        token = bearer_token
        if token is None and request is not None:
            token = request.cookies.get(settings.auth_session_cookie_name)
        if token is None:
            return _NO_TENANT_ID

        session = resolve_active_session(db, token)
        if session is None:
            return _NO_TENANT_ID

        # For browser UI requests (cookie auth), ignore tenant header to prevent
        # stale header values from causing tenant mismatch and redirect loops.
        # For explicit bearer-auth requests, preserve legacy override behavior.
        if bearer_token is not None and x_tenant_id is not None:
            return x_tenant_id
        return session.tenant_id

    if x_tenant_id is not None:
        return x_tenant_id

    token = _extract_bearer_token(authorization)
    if token is None and request is not None:
        token = request.cookies.get(settings.auth_session_cookie_name)
    if token is None:
        # Return a non-existent tenant id to avoid accidental global data exposure.
        return _NO_TENANT_ID

    session = resolve_active_session(db, token)
    if session is None:
        return _NO_TENANT_ID
    return session.tenant_id

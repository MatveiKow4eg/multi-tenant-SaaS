from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.services.auth.session_manager import resolve_active_session


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
    if x_tenant_id is not None:
        return x_tenant_id

    token = _extract_bearer_token(authorization)
    if token is None and request is not None:
        token = request.cookies.get(settings.auth_session_cookie_name)
    if token is None:
        return None

    session = resolve_active_session(db, token)
    if session is None:
        return None
    return session.tenant_id

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_tenant_id
from app.core.config import settings
from app.db.session import get_db
from app.models.tenant_membership import TenantMembership
from app.services.auth.session_manager import resolve_active_session


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        token = authorization[len(prefix):].strip()
        return token or None
    return None


def _role_value(role: object) -> str:
    return str(getattr(role, "value", role or ""))


def get_current_membership(
    tenant_id: int | None = Depends(get_tenant_id),
    authorization: str | None = Header(default=None, alias="Authorization"),
    request: Request = None,
    db: Session = Depends(get_db),
) -> TenantMembership | None:
    token = _extract_bearer_token(authorization)
    if token is None and request is not None:
        token = request.cookies.get(settings.auth_session_cookie_name)
    if token is None:
        if settings.auth_enforce_rbac:
            raise HTTPException(status_code=401, detail="missing_bearer_token")
        return None

    session = resolve_active_session(db, token)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid_or_expired_session")

    effective_tenant_id = tenant_id or session.tenant_id
    membership = (
        db.query(TenantMembership)
        .options(joinedload(TenantMembership.tenant))
        .filter(
            TenantMembership.user_id == session.user_id,
            TenantMembership.tenant_id == effective_tenant_id,
            TenantMembership.status == "active",
        )
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="membership_not_found")
    return membership


def require_roles(*allowed_roles: str, strict: bool = False) -> Callable:
    allowed = set(allowed_roles)

    def _dependency(
        membership: TenantMembership | None = Depends(get_current_membership),
    ) -> TenantMembership | None:
        if membership is None:
            if strict:
                raise HTTPException(status_code=401, detail="authentication_required")
            # Legacy mode: auth can be disabled via settings.auth_enforce_rbac.
            return None

        role = _role_value(membership.role)
        if role not in allowed:
            raise HTTPException(status_code=403, detail="insufficient_role")
        return membership

    return _dependency

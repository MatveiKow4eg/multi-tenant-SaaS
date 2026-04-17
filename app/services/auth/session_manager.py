from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.tenant_membership import TenantMembership
from app.models.user_session import UserSession
from app.services.auth.security import generate_session_token, hash_token


SESSION_TTL_DAYS = 30


def create_session(
    db: Session,
    *,
    user_id: int,
    tenant_id: int,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[UserSession, str]:
    raw_token = generate_session_token()
    session = UserSession(
        user_id=user_id,
        tenant_id=tenant_id,
        token_hash=hash_token(raw_token),
        user_agent=(user_agent or "")[:512] or None,
        ip_address=(ip_address or "")[:64] or None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session, raw_token


def resolve_active_session(db: Session, raw_token: str) -> UserSession | None:
    now = datetime.now(timezone.utc)
    token_hash = hash_token(raw_token)
    return (
        db.query(UserSession)
        .filter(
            UserSession.token_hash == token_hash,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .first()
    )


def revoke_session(db: Session, session: UserSession) -> None:
    session.revoked_at = datetime.now(timezone.utc)
    db.add(session)
    db.commit()


def revoke_all_user_sessions(db: Session, user_id: int) -> int:
    now = datetime.now(timezone.utc)
    rows = (
        db.query(UserSession)
        .filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .all()
    )
    for row in rows:
        row.revoked_at = now
        db.add(row)
    db.commit()
    return len(rows)


def rotate_session_if_needed(
    db: Session,
    session: UserSession,
    *,
    rotate_before_hours: int,
) -> str | None:
    now = datetime.now(timezone.utc)
    if session.revoked_at is not None or session.expires_at <= now:
        return None
    if session.expires_at - now > timedelta(hours=rotate_before_hours):
        return None

    # Re-issue a fresh session and revoke the near-expiry token.
    _, raw_token = create_session(
        db,
        user_id=session.user_id,
        tenant_id=session.tenant_id,
        user_agent=session.user_agent,
        ip_address=session.ip_address,
    )
    session.revoked_at = now
    db.add(session)
    db.commit()
    return raw_token


def list_user_memberships(db: Session, user_id: int) -> list[TenantMembership]:
    return db.query(TenantMembership).filter(TenantMembership.user_id == user_id).all()


def pick_membership(
    memberships: list[TenantMembership],
    tenant_slug: str | None,
) -> TenantMembership | None:
    active_memberships = [m for m in memberships if m.status == "active"]
    if not active_memberships:
        return None
    if tenant_slug:
        for membership in active_memberships:
            if membership.tenant and membership.tenant.slug == tenant_slug:
                return membership
        return None
    if len(active_memberships) == 1:
        return active_memberships[0]
    return None


def auth_payload(user: Any, memberships: list[TenantMembership]) -> dict[str, Any]:
    return {
        "user_id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "memberships": [
            {
                "tenant_id": membership.tenant_id,
                "tenant_slug": membership.tenant.slug if membership.tenant else "",
                "tenant_name": membership.tenant.name if membership.tenant else "",
                "role": membership.role,
            }
            for membership in memberships
        ],
    }

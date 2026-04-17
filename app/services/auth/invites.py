from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.tenant_invite import TenantInvite
from app.services.auth.security import generate_session_token, hash_token


def create_invite(
    db: Session,
    *,
    tenant_id: int,
    invited_by_user_id: int,
    email: str,
    role: str,
    expires_in_hours: int,
) -> tuple[TenantInvite, str]:
    raw_token = generate_session_token()
    invite = TenantInvite(
        tenant_id=tenant_id,
        invited_by_user_id=invited_by_user_id,
        email=email.strip().lower(),
        role=role,
        token_hash=hash_token(raw_token),
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=expires_in_hours),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite, raw_token


def resolve_pending_invite(db: Session, token: str) -> TenantInvite | None:
    now = datetime.now(timezone.utc)
    token_hash = hash_token(token)
    return (
        db.query(TenantInvite)
        .filter(
            TenantInvite.token_hash == token_hash,
            TenantInvite.status == "pending",
            TenantInvite.revoked_at.is_(None),
            TenantInvite.accepted_at.is_(None),
            TenantInvite.expires_at > now,
        )
        .first()
    )

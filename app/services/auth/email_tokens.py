from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.email_token import EmailToken
from app.services.auth.security import generate_session_token, hash_token

_EXPIRE_HOURS = {"verify_email": 72, "reset_password": 2}


def create_email_token(
    db: Session,
    *,
    user_id: int,
    purpose: str,
    expires_in_hours: int | None = None,
) -> str:
    """Create a single-use email token. Returns the raw (unhashed) token."""
    hours = expires_in_hours if expires_in_hours is not None else _EXPIRE_HOURS.get(purpose, 24)
    raw = generate_session_token()
    token = EmailToken(
        user_id=user_id,
        token_hash=hash_token(raw),
        purpose=purpose,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
    )
    db.add(token)
    db.commit()
    return raw


def consume_email_token(db: Session, raw_token: str, purpose: str) -> EmailToken | None:
    """Validate, mark used, and return the EmailToken row; or None if invalid."""
    now = datetime.now(timezone.utc)
    token_hash = hash_token(raw_token)
    row = (
        db.query(EmailToken)
        .filter(
            EmailToken.token_hash == token_hash,
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
            EmailToken.expires_at > now,
        )
        .first()
    )
    if row is None:
        return None
    row.used_at = now
    db.commit()
    return row

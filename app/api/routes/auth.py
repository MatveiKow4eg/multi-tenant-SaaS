import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.db.session import get_db
from app.models.tenant import Tenant
from app.models.tenant_invite import TenantInvite
from app.models.tenant_membership import MembershipRole, TenantMembership
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.auth import (
    AcceptInviteRequest,
    AuthSessionRead,
    ForgotPasswordRequest,
    LoginRequest,
    MeResponse,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
)
from app.services.auth.security import hash_password, verify_password
from app.services.auth.invites import resolve_pending_invite
from app.services.auth.email_tokens import create_email_token, consume_email_token
from app.services.auth.rate_limit import acquire_email_cooldown
from app.services.auth.rate_limit import clear_login_failures, is_login_allowed, register_login_failure
from app.services.mail.auth_emails import send_verification_email, send_password_reset_email
from app.services.auth.session_manager import (
    auth_payload,
    create_session,
    list_user_memberships,
    pick_membership,
    resolve_active_session,
    revoke_all_user_sessions,
    revoke_session,
)

router = APIRouter()


def _audit_auth_event(
    db: Session,
    *,
    action: str,
    user_id: int | None = None,
    reason: str | None = None,
    details: dict | None = None,
) -> None:
    try:
        db.add(
            AuditLog(
                entity_type="auth",
                entity_id=user_id,
                action=action,
                details=details,
                reason=reason,
            )
        )
        db.commit()
    except Exception:
        db.rollback()


def _slugify(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    value = value.strip("-")
    return value or "workspace"


def _ensure_unique_slug(db: Session, base_slug: str) -> str:
    slug = base_slug
    idx = 1
    while db.query(Tenant.id).filter(Tenant.slug == slug).first() is not None:
        idx += 1
        slug = f"{base_slug}-{idx}"
    return slug


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        token = authorization[len(prefix):].strip()
        return token or None
    return None


@router.post("/register", response_model=AuthSessionRead)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> AuthSessionRead:
    existing_user = db.query(User.id).filter(User.email == payload.email.lower()).first()
    if existing_user:
        raise HTTPException(status_code=409, detail="email_already_exists")

    base_slug = _slugify(payload.tenant_name)
    tenant = Tenant(name=payload.tenant_name.strip(), slug=_ensure_unique_slug(db, base_slug))
    db.add(tenant)
    db.flush()

    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        is_active=True,
        email_verified=False,
    )
    db.add(user)
    db.flush()

    db.add(
        TenantMembership(
            tenant_id=tenant.id,
            user_id=user.id,
            role=MembershipRole.owner,
            status="active",
        )
    )
    db.commit()

    # Send email verification
    raw_token = create_email_token(db, user_id=user.id, purpose="verify_email")
    verify_url = f"{settings.app_public_base_url}/ui/verify-email?token={raw_token}"
    try:
        send_verification_email(to_email=user.email, verify_url=verify_url)
    except Exception:
        pass  # Non-blocking: user can resend later

    _, token = create_session(
        db,
        user_id=user.id,
        tenant_id=tenant.id,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    return AuthSessionRead(token=token, tenant_id=tenant.id, tenant_slug=tenant.slug, user_id=user.id)


@router.post("/login", response_model=AuthSessionRead)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> AuthSessionRead:
    normalized_email = payload.email.lower().strip()
    if not is_login_allowed(email=normalized_email):
        _audit_auth_event(
            db,
            action="auth_login_blocked",
            reason="login_temporarily_locked",
            details={"email": normalized_email},
        )
        raise HTTPException(status_code=429, detail="login_temporarily_locked")

    user = db.query(User).filter(User.email == normalized_email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        locked_now = register_login_failure(
            email=normalized_email,
            max_attempts=settings.auth_login_max_attempts,
            window_seconds=settings.auth_login_attempt_window_seconds,
            lockout_seconds=settings.auth_login_lockout_seconds,
        )
        if locked_now:
            _audit_auth_event(
                db,
                action="auth_login_blocked",
                reason="login_lock_threshold_reached",
                details={"email": normalized_email},
            )
            raise HTTPException(status_code=429, detail="login_temporarily_locked")
        _audit_auth_event(
            db,
            action="auth_login_failed",
            reason="invalid_credentials",
            details={"email": normalized_email},
        )
        raise HTTPException(status_code=401, detail="invalid_credentials")

    clear_login_failures(email=normalized_email)
    if not user.is_active:
        _audit_auth_event(
            db,
            action="auth_login_denied",
            user_id=user.id,
            reason="user_inactive",
            details={"email": normalized_email},
        )
        raise HTTPException(status_code=403, detail="user_inactive")
    if settings.auth_require_email_verified and not user.email_verified:
        _audit_auth_event(
            db,
            action="auth_login_denied",
            user_id=user.id,
            reason="email_not_verified",
            details={"email": normalized_email},
        )
        raise HTTPException(status_code=403, detail="email_not_verified")

    memberships = (
        db.query(TenantMembership)
        .options(joinedload(TenantMembership.tenant))
        .filter(TenantMembership.user_id == user.id)
        .all()
    )
    membership = pick_membership(memberships, payload.tenant_slug)
    if membership is None:
        _audit_auth_event(
            db,
            action="auth_login_denied",
            user_id=user.id,
            reason="membership_not_found_or_ambiguous",
            details={"email": normalized_email},
        )
        raise HTTPException(status_code=403, detail="membership_not_found_or_ambiguous")

    _, token = create_session(
        db,
        user_id=user.id,
        tenant_id=membership.tenant_id,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _audit_auth_event(
        db,
        action="auth_login_success",
        user_id=user.id,
        details={"tenant_id": membership.tenant_id, "email": normalized_email},
    )
    return AuthSessionRead(
        token=token,
        tenant_id=membership.tenant_id,
        tenant_slug=membership.tenant.slug if membership.tenant else "",
        user_id=user.id,
    )


@router.get("/me", response_model=MeResponse)
def me(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> MeResponse:
    token = _extract_bearer_token(authorization)
    if not token:
        token = request.cookies.get(settings.auth_session_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="missing_bearer_token")

    session = resolve_active_session(db, token)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid_or_expired_session")

    user = db.query(User).filter(User.id == session.user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="user_not_found")

    memberships = (
        db.query(TenantMembership)
        .options(joinedload(TenantMembership.tenant))
        .filter(TenantMembership.user_id == user.id)
        .all()
    )
    return MeResponse(**auth_payload(user, memberships))


@router.post("/logout")
def logout(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    token = _extract_bearer_token(authorization)
    if not token:
        token = request.cookies.get(settings.auth_session_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="missing_bearer_token")

    session = resolve_active_session(db, token)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid_or_expired_session")

    revoke_session(db, session)
    _audit_auth_event(
        db,
        action="auth_logout",
        user_id=session.user_id,
        details={"tenant_id": session.tenant_id},
    )
    return {"ok": True}


@router.post("/logout-all")
def logout_all(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> dict[str, int | bool]:
    token = _extract_bearer_token(authorization)
    if not token:
        token = request.cookies.get(settings.auth_session_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="missing_bearer_token")

    session = resolve_active_session(db, token)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid_or_expired_session")

    revoked = revoke_all_user_sessions(db, session.user_id)
    _audit_auth_event(
        db,
        action="auth_logout_all",
        user_id=session.user_id,
        details={"revoked": revoked, "tenant_id": session.tenant_id},
    )
    return {"ok": True, "revoked": revoked}


@router.post("/accept-invite", response_model=AuthSessionRead)
def accept_invite(
    payload: AcceptInviteRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> AuthSessionRead:
    invite = resolve_pending_invite(db, payload.token)
    if invite is None:
        raise HTTPException(status_code=404, detail="invite_not_found_or_expired")

    user = db.query(User).filter(User.email == invite.email).first()
    if user is None:
        user = User(
            email=invite.email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        db.flush()
    else:
        user.password_hash = hash_password(payload.password)
        user.is_active = True
        user.email_verified = True
        if payload.full_name:
            user.full_name = payload.full_name
        db.add(user)

    membership = (
        db.query(TenantMembership)
        .filter(TenantMembership.tenant_id == invite.tenant_id, TenantMembership.user_id == user.id)
        .first()
    )
    if membership is None:
        membership = TenantMembership(
            tenant_id=invite.tenant_id,
            user_id=user.id,
            role=MembershipRole(invite.role),
            status="active",
        )
        db.add(membership)
    else:
        membership.role = MembershipRole(invite.role)
        membership.status = "active"
        db.add(membership)

    invite.status = "accepted"
    invite.accepted_at = datetime.now(timezone.utc)
    db.add(invite)
    db.commit()

    tenant = db.query(Tenant).filter(Tenant.id == invite.tenant_id).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")

    _, token = create_session(
        db,
        user_id=user.id,
        tenant_id=invite.tenant_id,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    return AuthSessionRead(
        token=token,
        tenant_id=invite.tenant_id,
        tenant_slug=tenant.slug,
        user_id=user.id,
    )


@router.get("/verify-email")
def verify_email(token: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    row = consume_email_token(db, token, "verify_email")
    if row is None:
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")
    user = db.query(User).filter(User.id == row.user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    user.email_verified = True
    db.commit()
    return {"ok": True}


@router.post("/resend-verification")
def resend_verification(
    payload: ResendVerificationRequest,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    normalized_email = payload.email.lower().strip()
    user = db.query(User).filter(User.email == normalized_email).first()
    sent = False
    if user and not user.email_verified and user.is_active:
        raw_token = create_email_token(db, user_id=user.id, purpose="verify_email")
        verify_url = f"{settings.app_public_base_url}/ui/verify-email?token={raw_token}"
        try:
            send_verification_email(to_email=user.email, verify_url=verify_url)
            sent = True
        except Exception:
            pass
    _audit_auth_event(
        db,
        action="auth_resend_verification_requested",
        user_id=user.id if user else None,
        details={"email": normalized_email, "sent": sent},
    )
    # Always respond OK to prevent user enumeration
    return {"ok": True}


@router.post("/forgot-password")
def forgot_password(
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    normalized_email = payload.email.lower().strip()
    user = db.query(User).filter(User.email == normalized_email).first()
    sent = False
    if user and user.is_active:
        can_send = acquire_email_cooldown(
            purpose="forgot-password",
            email=normalized_email,
            ttl_seconds=settings.auth_forgot_password_cooldown_seconds,
        )

        if can_send:
            raw_token = create_email_token(db, user_id=user.id, purpose="reset_password")
            reset_url = f"{settings.app_public_base_url}/ui/reset-password?token={raw_token}"
            try:
                send_password_reset_email(to_email=user.email, reset_url=reset_url)
                sent = True
            except Exception:
                pass
    _audit_auth_event(
        db,
        action="auth_forgot_password_requested",
        user_id=user.id if user else None,
        details={"email": normalized_email, "sent": sent},
    )
    # Always respond OK to prevent user enumeration
    return {"ok": True}


@router.post("/reset-password")
def reset_password(
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    row = consume_email_token(db, payload.token, "reset_password")
    if row is None:
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")
    user = db.query(User).filter(User.id == row.user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    user.password_hash = hash_password(payload.password)
    db.commit()
    _audit_auth_event(
        db,
        action="auth_password_reset_completed",
        user_id=user.id,
        details={"email": user.email},
    )
    return {"ok": True}

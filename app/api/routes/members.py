from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.api.security import require_roles
from app.db.session import get_db
from app.models.tenant_membership import MembershipRole, TenantMembership
from app.models.tenant_invite import TenantInvite
from app.models.user import User
from app.schemas.members import (
    TenantMemberCreateRequest,
    TenantInviteCreateRequest,
    TenantInviteRead,
    TenantMemberRead,
    TenantMemberUpdateRequest,
)
from app.services.auth.security import hash_password
from app.services.auth.invites import create_invite

router = APIRouter()

_ALLOWED_ROLES = {"owner", "admin", "manager", "operator", "viewer"}
_ALLOWED_STATUSES = {"active", "disabled"}


def _role_value(role: object) -> str:
    return str(getattr(role, "value", role or ""))


def _to_member_read(membership: TenantMembership, user: User) -> TenantMemberRead:
    return TenantMemberRead(
        id=membership.id,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=_role_value(membership.role),
        status=membership.status,
    )


@router.get("", response_model=list[TenantMemberRead])
def list_members(
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("viewer", "operator", "manager", "admin", "owner", strict=True)),
    db: Session = Depends(get_db),
) -> list[TenantMemberRead]:
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="tenant_not_selected")

    rows = (
        db.query(TenantMembership, User)
        .join(User, User.id == TenantMembership.user_id)
        .filter(TenantMembership.tenant_id == tenant_id)
        .order_by(TenantMembership.id.asc())
        .all()
    )
    return [_to_member_read(m, u) for m, u in rows]


@router.post("", response_model=TenantMemberRead)
def add_member(
    payload: TenantMemberCreateRequest,
    tenant_id: int | None = Depends(get_tenant_id),
    actor: TenantMembership = Depends(require_roles("admin", "owner", strict=True)),
    db: Session = Depends(get_db),
) -> TenantMemberRead:
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="tenant_not_selected")
    if actor.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="insufficient_role")

    role = payload.role.strip().lower()
    if role not in _ALLOWED_ROLES:
        raise HTTPException(status_code=422, detail="invalid_role")

    email = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        if not payload.password:
            raise HTTPException(status_code=422, detail="password_required_for_new_user")
        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
            is_active=True,
            email_verified=False,
        )
        db.add(user)
        db.flush()
    elif payload.full_name and not user.full_name:
        user.full_name = payload.full_name
        db.add(user)

    existing = (
        db.query(TenantMembership)
        .filter(TenantMembership.tenant_id == tenant_id, TenantMembership.user_id == user.id)
        .first()
    )
    if existing:
        existing.role = MembershipRole(role)
        existing.status = "active"
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return _to_member_read(existing, user)

    membership = TenantMembership(
        tenant_id=tenant_id,
        user_id=user.id,
        role=MembershipRole(role),
        status="active",
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return _to_member_read(membership, user)


@router.patch("/{membership_id}", response_model=TenantMemberRead)
def update_member(
    membership_id: int,
    payload: TenantMemberUpdateRequest,
    tenant_id: int | None = Depends(get_tenant_id),
    actor: TenantMembership = Depends(require_roles("admin", "owner", strict=True)),
    db: Session = Depends(get_db),
) -> TenantMemberRead:
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="tenant_not_selected")
    if actor.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="insufficient_role")

    row = (
        db.query(TenantMembership, User)
        .join(User, User.id == TenantMembership.user_id)
        .filter(TenantMembership.id == membership_id, TenantMembership.tenant_id == tenant_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="membership_not_found")

    membership, user = row

    actor_role = _role_value(actor.role)
    if actor_role != "owner" and _role_value(membership.role) == "owner":
        raise HTTPException(status_code=403, detail="owner_update_forbidden")

    if payload.role is not None:
        role = payload.role.strip().lower()
        if role not in _ALLOWED_ROLES:
            raise HTTPException(status_code=422, detail="invalid_role")
        if actor_role != "owner" and role == "owner":
            raise HTTPException(status_code=403, detail="owner_update_forbidden")
        membership.role = MembershipRole(role)

    if payload.status is not None:
        status = payload.status.strip().lower()
        if status not in _ALLOWED_STATUSES:
            raise HTTPException(status_code=422, detail="invalid_status")
        membership.status = status

    db.add(membership)
    db.commit()
    db.refresh(membership)
    return _to_member_read(membership, user)


@router.post("/invite", response_model=TenantInviteRead)
def invite_member(
    payload: TenantInviteCreateRequest,
    tenant_id: int | None = Depends(get_tenant_id),
    actor: TenantMembership = Depends(require_roles("admin", "owner", strict=True)),
    db: Session = Depends(get_db),
) -> TenantInviteRead:
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="tenant_not_selected")
    if actor.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="insufficient_role")

    role = payload.role.strip().lower()
    if role not in _ALLOWED_ROLES:
        raise HTTPException(status_code=422, detail="invalid_role")

    email = payload.email.strip().lower()
    existing_pending = (
        db.query(TenantInvite)
        .filter(
            TenantInvite.tenant_id == tenant_id,
            TenantInvite.email == email,
            TenantInvite.status == "pending",
        )
        .first()
    )
    if existing_pending:
        raise HTTPException(status_code=409, detail="pending_invite_exists")

    invite, raw_token = create_invite(
        db,
        tenant_id=tenant_id,
        invited_by_user_id=actor.user_id,
        email=email,
        role=role,
        expires_in_hours=payload.expires_in_hours,
    )
    return TenantInviteRead(
        id=invite.id,
        tenant_id=invite.tenant_id,
        email=invite.email,
        role=invite.role,
        status=invite.status,
        expires_at=invite.expires_at.astimezone(timezone.utc).isoformat(),
        invite_token=raw_token,
    )

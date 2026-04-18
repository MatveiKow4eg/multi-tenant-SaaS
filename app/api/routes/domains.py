"""Sender Domains API — DNS wizard endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_tenant_id
from app.api.security import require_roles
from app.db.session import get_db
from app.models.audit_log import AuditLog
from app.models.sender_domain import ManagedDkimSelector, SenderDomain
from app.services.sender_domains import create_sender_domain_profile, normalize_domain, rotate_dkim, verify_sender_domain

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DomainCreate(BaseModel):
    domain: str

    @field_validator("domain")
    @classmethod
    def normalise(cls, v: str) -> str:
        return normalize_domain(v)


class DnsRecordRead(BaseModel):
    id: int
    purpose: str
    record_type: str
    host: str
    value: str
    selector: str | None
    status: str
    actual_value: str | None
    error_message: str | None
    last_checked_at: datetime | None

    model_config = {"from_attributes": True}


class SenderDomainRead(BaseModel):
    id: int
    domain: str
    status: str
    dkim_mode: str
    is_default: bool
    send_from_email: str
    spf_status: str
    dkim_status: str
    dmarc_status: str
    send_enabled: bool
    created_at: datetime
    updated_at: datetime | None
    verified_at: datetime | None
    last_checked_at: datetime | None
    dns_records: list[DnsRecordRead]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_domain_or_404(domain_id: int, tenant_id: int, db: Session) -> SenderDomain:
    obj = (
        db.query(SenderDomain)
        .options(
            joinedload(SenderDomain.dns_records),
            joinedload(SenderDomain.dkim_keys),
            joinedload(SenderDomain.managed_selectors).joinedload(ManagedDkimSelector.dkim_key_pair),
        )
        .filter(SenderDomain.id == domain_id, SenderDomain.tenant_id == tenant_id)
        .first()
    )
    if not obj:
        raise HTTPException(status_code=404, detail="Domain not found")
    return obj


def _require_tenant(tenant_id: int | None) -> int:
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="X-Tenant-Id required")
    return tenant_id


def _recompute_overall_status(sd: SenderDomain) -> None:
    if sd.spf_status == "verified" and sd.dkim_status == "verified" and sd.dmarc_status == "verified":
        sd.status = "verified"
        sd.send_enabled = True
        if sd.verified_at is None:
            sd.verified_at = datetime.now(timezone.utc)
    elif any(s == "mismatch" for s in [sd.spf_status, sd.dkim_status, sd.dmarc_status]):
        sd.status = "failed"
        sd.send_enabled = False
    else:
        sd.status = "pending"
        sd.send_enabled = False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[SenderDomainRead])
def list_domains(
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> list[SenderDomain]:
    tid = _require_tenant(tenant_id)
    return (
        db.query(SenderDomain)
        .options(
            joinedload(SenderDomain.dns_records),
            joinedload(SenderDomain.dkim_keys),
            joinedload(SenderDomain.managed_selectors).joinedload(ManagedDkimSelector.dkim_key_pair),
        )
        .filter(SenderDomain.tenant_id == tid)
        .all()
    )


@router.post("/", response_model=SenderDomainRead, status_code=201)
def add_domain(
    payload: DomainCreate,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
    _roles=Depends(require_roles("owner", "admin")),
) -> SenderDomain:
    tid = _require_tenant(tenant_id)
    try:
        sender_domain = create_sender_domain_profile(db=db, tenant_id=tid, domain=payload.domain)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.add(AuditLog(
        tenant_id=tid,
        action="domain_added",
        entity_type="sender_domain",
        entity_id=sender_domain.id,
        details={"domain": sender_domain.domain, "dkim_mode": sender_domain.dkim_mode},
    ))
    db.commit()
    db.refresh(sender_domain)
    return sender_domain


@router.get("/{domain_id}", response_model=SenderDomainRead)
def get_domain(
    domain_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> SenderDomain:
    tid = _require_tenant(tenant_id)
    return _get_domain_or_404(domain_id, tid, db)


@router.post("/{domain_id}/check-dns", response_model=SenderDomainRead)
def check_dns(
    domain_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> SenderDomain:
    tid = _require_tenant(tenant_id)
    sd = _get_domain_or_404(domain_id, tid, db)
    verify_sender_domain(sd)

    db.add(AuditLog(
        tenant_id=tid,
        action="domain_dns_checked",
        entity_type="sender_domain",
        entity_id=sd.id,
        details={
            "domain": sd.domain,
            "spf": sd.spf_status,
            "dkim": sd.dkim_status,
            "dmarc": sd.dmarc_status,
            "overall": sd.status,
        },
    ))
    db.commit()
    db.refresh(sd)
    return sd


@router.post("/{domain_id}/regenerate-dkim", response_model=SenderDomainRead)
def regenerate_dkim(
    domain_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
    _roles=Depends(require_roles("owner", "admin")),
) -> SenderDomain:
    tid = _require_tenant(tenant_id)
    sd = _get_domain_or_404(domain_id, tid, db)
    rotate_dkim(sd, db)

    db.add(AuditLog(
        tenant_id=tid,
        action="domain_dkim_rotated",
        entity_type="sender_domain",
        entity_id=sd.id,
        details={"domain": sd.domain, "dkim_mode": sd.dkim_mode},
    ))
    db.commit()
    db.refresh(sd)
    return sd


@router.delete("/{domain_id}", status_code=200)
def delete_domain(
    domain_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
    _roles=Depends(require_roles("owner", "admin")),
) -> None:
    tid = _require_tenant(tenant_id)
    sd = (
        db.query(SenderDomain)
        .filter(SenderDomain.id == domain_id, SenderDomain.tenant_id == tid)
        .first()
    )
    if not sd:
        raise HTTPException(status_code=404, detail="Domain not found")

    db.add(AuditLog(
        tenant_id=tid, action="domain_deleted",
        entity_type="sender_domain", entity_id=domain_id,
        details={"domain": sd.domain},
    ))
    db.delete(sd)
    db.commit()

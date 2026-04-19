from __future__ import annotations

from datetime import datetime, timezone
import secrets

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.sender_domain import DkimKeyPair, ManagedDkimSelector, SenderDomain, SenderDomainDnsRecord
from app.services.dns.checker import RecordCheckResult, check_cname, check_dkim_txt, check_dmarc, check_spf, check_txt_contains
from app.services.dns.generator import (
    build_dkim_txt_value,
    build_dmarc_value,
    build_managed_dkim_target,
    build_spf_value,
    generate_dkim_keypair,
)


def normalize_domain(value: str) -> str:
    domain = value.lower().strip()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    return domain.rstrip("/")


def _purpose_status(records: list[SenderDomainDnsRecord]) -> str:
    if records and all(record.status == "verified" for record in records):
        return "verified"
    if any(record.status == "mismatch" for record in records):
        return "failed"
    return "pending"


def recompute_domain_status(sender_domain: SenderDomain) -> None:
    sender_domain.spf_status = _purpose_status([record for record in sender_domain.dns_records if record.purpose == "spf"])
    sender_domain.dkim_status = _purpose_status([record for record in sender_domain.dns_records if record.purpose == "dkim"])
    sender_domain.dmarc_status = _purpose_status([record for record in sender_domain.dns_records if record.purpose == "dmarc"])

    if sender_domain.spf_status == "verified" and sender_domain.dkim_status == "verified" and sender_domain.dmarc_status == "verified":
        sender_domain.status = "verified"
        sender_domain.send_enabled = True
        if sender_domain.verified_at is None:
            sender_domain.verified_at = datetime.now(timezone.utc)
    elif "failed" in {sender_domain.spf_status, sender_domain.dkim_status, sender_domain.dmarc_status}:
        sender_domain.status = "failed"
        sender_domain.send_enabled = False
    else:
        sender_domain.status = "pending"
        sender_domain.send_enabled = False


def authentication_status(sender_domain: SenderDomain) -> str:
    if sender_domain.spf_status == "verified" and sender_domain.dkim_status == "verified" and sender_domain.dmarc_status == "verified":
        return "authenticated"
    if "failed" in {sender_domain.spf_status, sender_domain.dkim_status, sender_domain.dmarc_status}:
        return "failed"
    return "pending"


def ensure_ownership_token(sender_domain: SenderDomain) -> None:
    if sender_domain.ownership_host is None or not sender_domain.ownership_host.strip():
        sender_domain.ownership_host = "_lertisento-verify"
    if sender_domain.ownership_token is None or not sender_domain.ownership_token.strip():
        sender_domain.ownership_token = f"lertisento-site-verification={secrets.token_urlsafe(20)}"
    if not sender_domain.ownership_status:
        sender_domain.ownership_status = "pending"
    if not sender_domain.ownership_email_status:
        sender_domain.ownership_email_status = "pending"
    if not sender_domain.ownership_dns_status:
        sender_domain.ownership_dns_status = "pending"


def recompute_ownership_status(sender_domain: SenderDomain) -> None:
    email_ok = sender_domain.ownership_email_status == "verified"
    dns_ok = sender_domain.ownership_dns_status == "verified"

    if email_ok or dns_ok:
        sender_domain.ownership_status = "verified"
        if email_ok:
            sender_domain.ownership_verified_via = "email"
            sender_domain.ownership_method = "email"
        else:
            sender_domain.ownership_verified_via = "dns"
            sender_domain.ownership_method = "dns"
        return

    if sender_domain.ownership_email_status == "failed" and sender_domain.ownership_dns_status == "failed":
        sender_domain.ownership_status = "failed"
        return

    sender_domain.ownership_status = "pending"


def mark_sender_domain_ownership_email_verified(sender_domain: SenderDomain, email: str) -> None:
    ensure_ownership_token(sender_domain)
    now = datetime.now(timezone.utc)
    sender_domain.ownership_email_status = "verified"
    sender_domain.ownership_email = email.strip()
    sender_domain.ownership_email_verified_at = now
    if sender_domain.ownership_verified_at is None:
        sender_domain.ownership_verified_at = now
    sender_domain.ownership_last_checked_at = now
    sender_domain.updated_at = now
    recompute_ownership_status(sender_domain)


def _initialize_sender_domain_profile(db: Session, sender_domain: SenderDomain) -> None:
    ensure_ownership_token(sender_domain)

    db.add(SenderDomainDnsRecord(
        sender_domain_id=sender_domain.id,
        purpose="spf",
        record_type="TXT",
        host="@",
        value=build_spf_value(),
    ))
    db.add(SenderDomainDnsRecord(
        sender_domain_id=sender_domain.id,
        purpose="dmarc",
        record_type="TXT",
        host="_dmarc",
        value=build_dmarc_value(),
    ))

    if sender_domain.dkim_mode == "cname":
        _create_managed_dkim_records(db, sender_domain)
    else:
        _create_txt_dkim_record(db, sender_domain, selector="default")


def create_sender_domain_profile(db: Session, tenant_id: int, domain: str) -> SenderDomain:
    domain = normalize_domain(domain)
    any_domain = (
        db.query(SenderDomain)
        .filter(SenderDomain.tenant_id == tenant_id)
        .first()
    )
    if any_domain:
        raise ValueError("Only one sender domain is allowed per account")

    existing = (
        db.query(SenderDomain)
        .filter(SenderDomain.tenant_id == tenant_id, SenderDomain.domain == domain)
        .first()
    )
    if existing:
        raise ValueError("Domain already registered")

    has_default = db.query(SenderDomain).filter(SenderDomain.tenant_id == tenant_id, SenderDomain.is_default.is_(True)).first()
    sender_domain = SenderDomain(
        tenant_id=tenant_id,
        domain=domain,
        ownership_status="pending",
        ownership_method="dns",
        ownership_host="_lertisento-verify",
        dkim_mode=settings.sender_domain_default_dkim_mode,
        from_local_part=settings.sender_default_local_part,
        is_default=has_default is None,
    )
    db.add(sender_domain)
    db.flush()
    _initialize_sender_domain_profile(db, sender_domain)

    return sender_domain


def sync_sender_domain_profile(db: Session, tenant_id: int, domain: str) -> tuple[SenderDomain, bool]:
    domain = normalize_domain(domain)
    sender_domain = (
        db.query(SenderDomain)
        .filter(SenderDomain.tenant_id == tenant_id)
        .order_by(SenderDomain.id.asc())
        .first()
    )
    if sender_domain is None:
        return create_sender_domain_profile(db=db, tenant_id=tenant_id, domain=domain), True

    domain_changed = sender_domain.domain != domain
    if domain_changed:
        db.query(ManagedDkimSelector).filter(ManagedDkimSelector.sender_domain_id == sender_domain.id).delete(synchronize_session=False)
        db.query(DkimKeyPair).filter(DkimKeyPair.sender_domain_id == sender_domain.id).delete(synchronize_session=False)
        db.query(SenderDomainDnsRecord).filter(SenderDomainDnsRecord.sender_domain_id == sender_domain.id).delete(synchronize_session=False)

        sender_domain.domain = domain
        sender_domain.ownership_status = "pending"
        sender_domain.ownership_method = "dns"
        sender_domain.ownership_verified_via = None
        sender_domain.ownership_email_status = "pending"
        sender_domain.ownership_dns_status = "pending"
        sender_domain.ownership_email = None
        sender_domain.ownership_token = None
        sender_domain.ownership_host = "_lertisento-verify"
        sender_domain.ownership_verified_at = None
        sender_domain.ownership_email_verified_at = None
        sender_domain.ownership_dns_verified_at = None
        sender_domain.ownership_last_checked_at = None
        sender_domain.status = "pending"
        sender_domain.spf_status = "pending"
        sender_domain.dkim_status = "pending"
        sender_domain.dmarc_status = "pending"
        sender_domain.send_enabled = False
        sender_domain.verified_at = None
        sender_domain.last_checked_at = None
        sender_domain.updated_at = datetime.now(timezone.utc)
        _initialize_sender_domain_profile(db, sender_domain)
        return sender_domain, True

    ensure_ownership_token(sender_domain)
    if not sender_domain.dns_records:
        sender_domain.updated_at = datetime.now(timezone.utc)
        _initialize_sender_domain_profile(db, sender_domain)
        return sender_domain, True
    return sender_domain, False


def _create_txt_dkim_record(db: Session, sender_domain: SenderDomain, selector: str) -> None:
    selector, encrypted_pem, public_key = generate_dkim_keypair(selector=selector)
    db.add(DkimKeyPair(
        sender_domain_id=sender_domain.id,
        selector=selector,
        private_key_encrypted=encrypted_pem,
        public_key=public_key,
        algorithm="rsa2048",
        active=True,
    ))
    db.add(SenderDomainDnsRecord(
        sender_domain_id=sender_domain.id,
        purpose="dkim",
        record_type="TXT",
        host=f"{selector}._domainkey",
        value=build_dkim_txt_value(public_key),
        selector=selector,
    ))


def _create_managed_dkim_records(db: Session, sender_domain: SenderDomain) -> None:
    active_selector = "s1"
    for selector in ("s1", "s2"):
        selector_name, encrypted_pem, public_key = generate_dkim_keypair(selector=selector)
        key_pair = DkimKeyPair(
            sender_domain_id=sender_domain.id,
            selector=selector_name,
            private_key_encrypted=encrypted_pem,
            public_key=public_key,
            algorithm="rsa2048",
            active=selector_name == active_selector,
        )
        db.add(key_pair)
        db.flush()

        target = build_managed_dkim_target(sender_domain_id=sender_domain.id, selector=selector_name)
        db.add(ManagedDkimSelector(
            sender_domain_id=sender_domain.id,
            dkim_key_pair_id=key_pair.id,
            selector=selector_name,
            cname_target=target,
            active=selector_name == active_selector,
        ))
        db.add(SenderDomainDnsRecord(
            sender_domain_id=sender_domain.id,
            purpose="dkim",
            record_type="CNAME",
            host=f"{selector_name}._domainkey",
            value=target,
            selector=selector_name,
        ))


def verify_sender_domain(sender_domain: SenderDomain) -> None:
    now = datetime.now(timezone.utc)
    for record in sender_domain.dns_records:
        result = _check_record(sender_domain, record)
        record.status = result.status
        record.actual_value = result.actual_value
        record.error_message = result.error_message
        record.last_checked_at = now

    sender_domain.last_checked_at = now
    sender_domain.updated_at = now
    recompute_domain_status(sender_domain)


def verify_sender_domain_ownership_dns(sender_domain: SenderDomain) -> RecordCheckResult:
    ensure_ownership_token(sender_domain)
    now = datetime.now(timezone.utc)
    ownership_host = sender_domain.ownership_host
    if ownership_host == "@":
        fqdn = sender_domain.domain
    else:
        fqdn = f"{ownership_host}.{sender_domain.domain}" if not ownership_host.endswith(f".{sender_domain.domain}") else ownership_host

    result = check_txt_contains(fqdn, sender_domain.ownership_token or "")
    sender_domain.ownership_last_checked_at = now
    if result.status == "verified":
        sender_domain.ownership_dns_status = "verified"
        sender_domain.ownership_dns_verified_at = now
        if sender_domain.ownership_verified_at is None:
            sender_domain.ownership_verified_at = now
    elif result.status in {"mismatch", "missing"}:
        sender_domain.ownership_dns_status = "failed" if result.status == "mismatch" else "pending"
    else:
        sender_domain.ownership_dns_status = "pending"

    sender_domain.updated_at = now
    recompute_ownership_status(sender_domain)
    return result


def _check_record(sender_domain: SenderDomain, record: SenderDomainDnsRecord) -> RecordCheckResult:
    if record.purpose == "spf":
        return check_spf(sender_domain.domain, record.value)
    if record.purpose == "dmarc":
        return check_dmarc(sender_domain.domain, record.value)
    if record.record_type == "CNAME":
        hostname = f"{record.host}.{sender_domain.domain}"
        return check_cname(hostname, record.value)
    return check_dkim_txt(sender_domain.domain, record.selector or "default", record.value)


def rotate_dkim(sender_domain: SenderDomain, db: Session) -> None:
    if sender_domain.dkim_mode == "cname":
        _rotate_managed_dkim(sender_domain, db)
        return

    # TXT fallback requires customer DNS update.
    for key in sender_domain.dkim_keys:
        key.active = False
    for record in sender_domain.dns_records:
        if record.purpose == "dkim":
            selector, encrypted_pem, public_key = generate_dkim_keypair(selector=record.selector or "default")
            db.add(DkimKeyPair(
                sender_domain_id=sender_domain.id,
                selector=selector,
                private_key_encrypted=encrypted_pem,
                public_key=public_key,
                algorithm="rsa2048",
                active=True,
            ))
            record.value = build_dkim_txt_value(public_key)
            record.status = "pending"
            record.actual_value = None
            record.error_message = None
            sender_domain.dkim_status = "pending"
            sender_domain.status = "pending"
            sender_domain.send_enabled = False
            sender_domain.updated_at = datetime.now(timezone.utc)
            break


def _rotate_managed_dkim(sender_domain: SenderDomain, db: Session) -> None:
    selectors = sorted(sender_domain.managed_selectors, key=lambda item: item.selector)
    if not selectors:
        _create_managed_dkim_records(db, sender_domain)
        return

    current = next((selector for selector in selectors if selector.active), selectors[0])
    target_selector = next((selector for selector in selectors if selector.id != current.id), current)

    for selector in selectors:
        selector.active = selector.id == target_selector.id
    for key in sender_domain.dkim_keys:
        key.active = False

    selector_name, encrypted_pem, public_key = generate_dkim_keypair(selector=target_selector.selector)
    new_key = DkimKeyPair(
        sender_domain_id=sender_domain.id,
        selector=selector_name,
        private_key_encrypted=encrypted_pem,
        public_key=public_key,
        algorithm="rsa2048",
        active=True,
    )
    db.add(new_key)
    db.flush()
    target_selector.dkim_key_pair_id = new_key.id
    target_selector.active = True
    sender_domain.updated_at = datetime.now(timezone.utc)


def resolve_sender_identity(db: Session, tenant_id: int) -> SenderDomain | None:
    sender_domain = (
        db.query(SenderDomain)
        .filter(
            SenderDomain.tenant_id == tenant_id,
            SenderDomain.send_enabled.is_(True),
            SenderDomain.is_default.is_(True),
        )
        .first()
    )
    if sender_domain is not None:
        return sender_domain
    return (
        db.query(SenderDomain)
        .filter(SenderDomain.tenant_id == tenant_id, SenderDomain.send_enabled.is_(True))
        .order_by(SenderDomain.id.asc())
        .first()
    )


def resolve_active_dkim_key(sender_domain: SenderDomain) -> tuple[str, str] | None:
    if sender_domain.dkim_mode == "cname":
        selector = next((item for item in sender_domain.managed_selectors if item.active and item.dkim_key_pair), None)
        if selector and selector.dkim_key_pair:
            return selector.selector, selector.dkim_key_pair.private_key_encrypted
    key = next((item for item in sender_domain.dkim_keys if item.active), None)
    if key is not None:
        return key.selector, key.private_key_encrypted
    return None

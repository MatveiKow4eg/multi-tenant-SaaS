from __future__ import annotations

import argparse
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.db.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.sender_domain import SenderDomain


def _normalize_domain(raw: str) -> str:
    value = (raw or "").strip().lower()
    if not value:
        raise ValueError("Domain is required")

    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").strip().lower()
        value = host

    if value.startswith("www."):
        value = value[4:]

    return value.rstrip("/")


def _replacement_domain(prefix: str, row_id: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{row_id}-{stamp}.invalid"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Support utility: release a blocked domain from trash/test account data.",
    )
    parser.add_argument("--domain", required=True, help="Domain to release, e.g. drivenbyfaith.eu")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply changes. Without this flag script runs in dry-run mode.",
    )
    args = parser.parse_args()

    target_domain = _normalize_domain(args.domain)

    db = SessionLocal()
    try:
        companies = db.query(Company).filter(Company.domain == target_domain).order_by(Company.id.asc()).all()
        sender_domains = (
            db.query(SenderDomain)
            .filter(SenderDomain.domain == target_domain)
            .order_by(SenderDomain.id.asc())
            .all()
        )

        print(f"Target domain: {target_domain}")
        print(f"Found companies: {len(companies)}")
        print(f"Found sender domains: {len(sender_domains)}")

        for item in companies:
            print(f"  Company id={item.id} tenant_id={item.tenant_id} name={item.name!r} domain={item.domain}")
        for item in sender_domains:
            print(f"  SenderDomain id={item.id} tenant_id={item.tenant_id} domain={item.domain}")

        if not companies and not sender_domains:
            print("Nothing to release.")
            return 0

        if not args.execute:
            print("Dry-run only. Re-run with --execute to apply changes.")
            return 0

        now = datetime.now(timezone.utc)

        for item in companies:
            old = item.domain
            item.domain = _replacement_domain("released-company", item.id)
            db.add(
                AuditLog(
                    tenant_id=item.tenant_id,
                    action="support_domain_released_company",
                    entity_type="company",
                    entity_id=item.id,
                    details={"old_domain": old, "new_domain": item.domain},
                )
            )

        for item in sender_domains:
            old = item.domain
            item.domain = _replacement_domain("released-sender", item.id)
            item.updated_at = now
            db.add(
                AuditLog(
                    tenant_id=item.tenant_id,
                    action="support_domain_released_sender_domain",
                    entity_type="sender_domain",
                    entity_id=item.id,
                    details={"old_domain": old, "new_domain": item.domain},
                )
            )

        db.commit()
        print("Domain released successfully.")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"Failed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

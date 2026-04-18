"""Backfill tenant_id for existing audit_log rows.

Revision ID: 20260417_0007
Revises: 20260417_0006
Create Date: 2026-04-17 00:20:00
"""

from __future__ import annotations

from collections.abc import Sequence
import json

from alembic import op
import sqlalchemy as sa

revision: str = "20260417_0007"
down_revision: str | None = "20260417_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _safe_int(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tenant_from_details(details: object | None) -> int | None:
    if details is None:
        return None

    payload = details
    if isinstance(details, str):
        try:
            payload = json.loads(details)
        except Exception:
            return None

    if isinstance(payload, dict):
        return _safe_int(payload.get("tenant_id"))
    return None


def upgrade() -> None:
    bind = op.get_bind()
    meta = sa.MetaData()

    audit_log = sa.Table("audit_log", meta, autoload_with=bind)
    companies = sa.Table("companies", meta, autoload_with=bind)
    campaigns = sa.Table("campaigns", meta, autoload_with=bind)
    messages = sa.Table("messages", meta, autoload_with=bind)
    replies = sa.Table("replies", meta, autoload_with=bind)
    handoffs = sa.Table("handoffs", meta, autoload_with=bind)
    tenant_memberships = sa.Table("tenant_memberships", meta, autoload_with=bind)

    company_cache: dict[int, int | None] = {}
    campaign_cache: dict[int, int | None] = {}
    message_cache: dict[int, int | None] = {}
    reply_cache: dict[int, int | None] = {}
    handoff_cache: dict[int, int | None] = {}
    auth_user_cache: dict[int, int | None] = {}

    def company_tenant(company_id: int | None) -> int | None:
        key = _safe_int(company_id)
        if key is None:
            return None
        if key not in company_cache:
            row = bind.execute(
                sa.select(companies.c.tenant_id).where(companies.c.id == key)
            ).first()
            company_cache[key] = _safe_int(row[0]) if row else None
        return company_cache[key]

    def campaign_tenant(campaign_id: int | None) -> int | None:
        key = _safe_int(campaign_id)
        if key is None:
            return None
        if key not in campaign_cache:
            row = bind.execute(
                sa.select(companies.c.tenant_id)
                .select_from(campaigns.join(companies, campaigns.c.company_id == companies.c.id))
                .where(campaigns.c.id == key)
            ).first()
            campaign_cache[key] = _safe_int(row[0]) if row else None
        return campaign_cache[key]

    def message_tenant(message_id: int | None) -> int | None:
        key = _safe_int(message_id)
        if key is None:
            return None
        if key not in message_cache:
            row = bind.execute(
                sa.select(companies.c.tenant_id)
                .select_from(
                    messages.join(campaigns, messages.c.campaign_id == campaigns.c.id).join(
                        companies, campaigns.c.company_id == companies.c.id
                    )
                )
                .where(messages.c.id == key)
            ).first()
            message_cache[key] = _safe_int(row[0]) if row else None
        return message_cache[key]

    def reply_tenant(reply_id: int | None) -> int | None:
        key = _safe_int(reply_id)
        if key is None:
            return None
        if key not in reply_cache:
            row = bind.execute(
                sa.select(companies.c.tenant_id)
                .select_from(
                    replies.join(messages, replies.c.message_id == messages.c.id)
                    .join(campaigns, messages.c.campaign_id == campaigns.c.id)
                    .join(companies, campaigns.c.company_id == companies.c.id)
                )
                .where(replies.c.id == key)
            ).first()
            reply_cache[key] = _safe_int(row[0]) if row else None
        return reply_cache[key]

    def handoff_tenant(handoff_id: int | None) -> int | None:
        key = _safe_int(handoff_id)
        if key is None:
            return None
        if key not in handoff_cache:
            row = bind.execute(
                sa.select(handoffs.c.company_id, handoffs.c.campaign_id).where(handoffs.c.id == key)
            ).first()
            if not row:
                handoff_cache[key] = None
            else:
                from_company = company_tenant(row[0])
                handoff_cache[key] = from_company if from_company is not None else campaign_tenant(row[1])
        return handoff_cache[key]

    def auth_tenant(user_id: int | None, details: object | None) -> int | None:
        details_tenant = _tenant_from_details(details)
        if details_tenant is not None:
            return details_tenant

        key = _safe_int(user_id)
        if key is None:
            return None
        if key not in auth_user_cache:
            row = bind.execute(
                sa.select(tenant_memberships.c.tenant_id)
                .where(
                    tenant_memberships.c.user_id == key,
                    tenant_memberships.c.status == "active",
                )
                .order_by(tenant_memberships.c.id.asc())
                .limit(1)
            ).first()
            auth_user_cache[key] = _safe_int(row[0]) if row else None
        return auth_user_cache[key]

    rows = bind.execute(
        sa.select(
            audit_log.c.id,
            audit_log.c.entity_type,
            audit_log.c.entity_id,
            audit_log.c.details,
        ).where(audit_log.c.tenant_id.is_(None))
    ).mappings().all()

    for row in rows:
        entity_type = (row["entity_type"] or "").lower()
        entity_id = _safe_int(row["entity_id"])
        details = row["details"]

        tenant_id: int | None = _tenant_from_details(details)

        if tenant_id is None:
            if entity_type == "auth":
                tenant_id = auth_tenant(entity_id, details)
            elif entity_type == "company":
                tenant_id = company_tenant(entity_id)
            elif entity_type == "campaign":
                tenant_id = campaign_tenant(entity_id)
            elif entity_type == "message":
                tenant_id = message_tenant(entity_id)
            elif entity_type == "reply":
                tenant_id = reply_tenant(entity_id)
            elif entity_type == "handoff":
                tenant_id = handoff_tenant(entity_id)

        if tenant_id is not None:
            bind.execute(
                audit_log.update().where(audit_log.c.id == row["id"]).values(tenant_id=tenant_id)
            )


def downgrade() -> None:
    # Backfill is irreversible by design.
    pass

"""Add sender_domains table for DNS wizard.

Revision ID: 20260418_0008
Revises: 20260417_0007
Create Date: 2026-04-18 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260418_0008"
down_revision: str | None = "20260417_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sender_domains",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("spf_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("dkim_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("dmarc_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("spf_value", sa.String(512), nullable=True),
        sa.Column("dkim_selector", sa.String(64), nullable=True, server_default="default"),
        sa.Column("dkim_value", sa.String(512), nullable=True),
        sa.Column("dmarc_value", sa.String(512), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sender_domains_tenant_id", "sender_domains", ["tenant_id"])
    op.create_index("ix_sender_domains_domain", "sender_domains", ["domain"])


def downgrade() -> None:
    op.drop_index("ix_sender_domains_domain", table_name="sender_domains")
    op.drop_index("ix_sender_domains_tenant_id", table_name="sender_domains")
    op.drop_table("sender_domains")

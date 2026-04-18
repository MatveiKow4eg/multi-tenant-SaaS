"""Expand sender_domains: add dns_records, dkim_key_pairs tables.

Revision ID: 20260418_0009
Revises: 20260418_0008
Create Date: 2026-04-18 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260418_0009"
down_revision: str | None = "20260418_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- alter sender_domains: add new columns, drop old ones ----
    with op.batch_alter_table("sender_domains") as batch:
        batch.add_column(sa.Column("status", sa.String(16), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("dkim_mode", sa.String(16), nullable=False, server_default="txt"))
        batch.add_column(sa.Column("send_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
        # drop old flat columns
        batch.drop_column("spf_value")
        batch.drop_column("dkim_selector")
        batch.drop_column("dkim_value")
        batch.drop_column("dmarc_value")

    # ---- sender_domain_dns_records ----
    op.create_table(
        "sender_domain_dns_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sender_domain_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("record_type", sa.String(8), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("selector", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("actual_value", sa.Text(), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["sender_domain_id"], ["sender_domains.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sddr_sender_domain_id", "sender_domain_dns_records", ["sender_domain_id"])

    # ---- dkim_key_pairs ----
    op.create_table(
        "dkim_key_pairs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sender_domain_id", sa.Integer(), nullable=False),
        sa.Column("selector", sa.String(64), nullable=False),
        sa.Column("private_key_encrypted", sa.Text(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("algorithm", sa.String(8), nullable=False, server_default="rsa2048"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["sender_domain_id"], ["sender_domains.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dkim_key_pairs_sender_domain_id", "dkim_key_pairs", ["sender_domain_id"])


def downgrade() -> None:
    op.drop_table("dkim_key_pairs")
    op.drop_table("sender_domain_dns_records")
    with op.batch_alter_table("sender_domains") as batch:
        batch.drop_column("verified_at")
        batch.drop_column("updated_at")
        batch.drop_column("send_enabled")
        batch.drop_column("dkim_mode")
        batch.drop_column("status")
        batch.add_column(sa.Column("spf_value", sa.String(512), nullable=True))
        batch.add_column(sa.Column("dkim_selector", sa.String(64), nullable=True, server_default="default"))
        batch.add_column(sa.Column("dkim_value", sa.String(512), nullable=True))
        batch.add_column(sa.Column("dmarc_value", sa.String(512), nullable=True))

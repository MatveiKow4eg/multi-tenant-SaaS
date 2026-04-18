"""Add ownership method-specific verification fields.

Revision ID: 20260418_0012
Revises: 20260418_0011
Create Date: 2026-04-18 19:25:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260418_0012"
down_revision: str | None = "20260418_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sender_domains") as batch:
        batch.add_column(sa.Column("ownership_verified_via", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("ownership_email_status", sa.String(length=16), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("ownership_dns_status", sa.String(length=16), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("ownership_email", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("ownership_email_verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("ownership_dns_verified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sender_domains") as batch:
        batch.drop_column("ownership_dns_verified_at")
        batch.drop_column("ownership_email_verified_at")
        batch.drop_column("ownership_email")
        batch.drop_column("ownership_dns_status")
        batch.drop_column("ownership_email_status")
        batch.drop_column("ownership_verified_via")

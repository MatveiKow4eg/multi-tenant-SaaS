"""Add sender domain ownership verification fields.

Revision ID: 20260418_0011
Revises: 20260418_0010
Create Date: 2026-04-18 18:40:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260418_0011"
down_revision: str | None = "20260418_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sender_domains") as batch:
        batch.add_column(sa.Column("ownership_status", sa.String(length=16), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("ownership_method", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("ownership_token", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("ownership_host", sa.String(length=255), nullable=False, server_default="_lertisento-verify"))
        batch.add_column(sa.Column("ownership_verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("ownership_last_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sender_domains") as batch:
        batch.drop_column("ownership_last_checked_at")
        batch.drop_column("ownership_verified_at")
        batch.drop_column("ownership_host")
        batch.drop_column("ownership_token")
        batch.drop_column("ownership_method")
        batch.drop_column("ownership_status")

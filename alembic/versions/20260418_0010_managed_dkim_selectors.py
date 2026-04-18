"""Add managed DKIM selector mapping and sender identity fields.

Revision ID: 20260418_0010
Revises: 20260418_0009
Create Date: 2026-04-18 14:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260418_0010"
down_revision: str | None = "20260418_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sender_domains") as batch:
        batch.add_column(sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("from_local_part", sa.String(length=128), nullable=False, server_default="hello"))
        batch.alter_column("dkim_mode", server_default="cname")

    op.create_table(
        "managed_dkim_selectors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sender_domain_id", sa.Integer(), nullable=False),
        sa.Column("dkim_key_pair_id", sa.Integer(), nullable=True),
        sa.Column("selector", sa.String(length=64), nullable=False),
        sa.Column("cname_target", sa.String(length=255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dkim_key_pair_id"], ["dkim_key_pairs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sender_domain_id"], ["sender_domains.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_managed_dkim_selectors_sender_domain_id", "managed_dkim_selectors", ["sender_domain_id"])
    op.create_index("ix_managed_dkim_selectors_dkim_key_pair_id", "managed_dkim_selectors", ["dkim_key_pair_id"])


def downgrade() -> None:
    op.drop_index("ix_managed_dkim_selectors_dkim_key_pair_id", table_name="managed_dkim_selectors")
    op.drop_index("ix_managed_dkim_selectors_sender_domain_id", table_name="managed_dkim_selectors")
    op.drop_table("managed_dkim_selectors")

    with op.batch_alter_table("sender_domains") as batch:
        batch.alter_column("dkim_mode", server_default="txt")
        batch.drop_column("from_local_part")
        batch.drop_column("is_default")

"""Add tenant_id column to audit_log

Revision ID: 20260417_0006
Revises: 20260417_0005
Create Date: 2026-04-17 00:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260417_0006"
down_revision: str | None = "20260417_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column("tenant_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_log_tenant_id", table_name="audit_log")
    op.drop_column("audit_log", "tenant_id")

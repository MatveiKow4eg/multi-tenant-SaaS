"""tenant invites

Revision ID: 20260417_0004
Revises: 20260417_0003
Create Date: 2026-04-17 02:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260417_0004"
down_revision: str | None = "20260417_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("invited_by_user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_tenant_invites_id", "tenant_invites", ["id"], unique=False)
    op.create_index("ix_tenant_invites_tenant_id", "tenant_invites", ["tenant_id"], unique=False)
    op.create_index("ix_tenant_invites_invited_by_user_id", "tenant_invites", ["invited_by_user_id"], unique=False)
    op.create_index("ix_tenant_invites_email", "tenant_invites", ["email"], unique=False)
    op.create_index("ix_tenant_invites_token_hash", "tenant_invites", ["token_hash"], unique=False)
    op.create_index("ix_tenant_invites_status", "tenant_invites", ["status"], unique=False)
    op.create_index("ix_tenant_invites_expires_at", "tenant_invites", ["expires_at"], unique=False)
    op.create_index("ix_tenant_invites_accepted_at", "tenant_invites", ["accepted_at"], unique=False)
    op.create_index("ix_tenant_invites_revoked_at", "tenant_invites", ["revoked_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tenant_invites_revoked_at", table_name="tenant_invites")
    op.drop_index("ix_tenant_invites_accepted_at", table_name="tenant_invites")
    op.drop_index("ix_tenant_invites_expires_at", table_name="tenant_invites")
    op.drop_index("ix_tenant_invites_status", table_name="tenant_invites")
    op.drop_index("ix_tenant_invites_token_hash", table_name="tenant_invites")
    op.drop_index("ix_tenant_invites_email", table_name="tenant_invites")
    op.drop_index("ix_tenant_invites_invited_by_user_id", table_name="tenant_invites")
    op.drop_index("ix_tenant_invites_tenant_id", table_name="tenant_invites")
    op.drop_index("ix_tenant_invites_id", table_name="tenant_invites")
    op.drop_table("tenant_invites")

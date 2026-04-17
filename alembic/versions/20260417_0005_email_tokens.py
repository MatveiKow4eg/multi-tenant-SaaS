"""email tokens for verification and password reset

Revision ID: 20260417_0005
Revises: 20260417_0004
Create Date: 2026-04-17 03:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260417_0005"
down_revision: str | None = "20260417_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_email_tokens_id", "email_tokens", ["id"], unique=False)
    op.create_index("ix_email_tokens_user_id", "email_tokens", ["user_id"], unique=False)
    op.create_index("ix_email_tokens_token_hash", "email_tokens", ["token_hash"], unique=False)
    op.create_index("ix_email_tokens_purpose", "email_tokens", ["purpose"], unique=False)
    op.create_index("ix_email_tokens_expires_at", "email_tokens", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_email_tokens_expires_at", table_name="email_tokens")
    op.drop_index("ix_email_tokens_purpose", table_name="email_tokens")
    op.drop_index("ix_email_tokens_token_hash", table_name="email_tokens")
    op.drop_index("ix_email_tokens_user_id", table_name="email_tokens")
    op.drop_index("ix_email_tokens_id", table_name="email_tokens")
    op.drop_table("email_tokens")

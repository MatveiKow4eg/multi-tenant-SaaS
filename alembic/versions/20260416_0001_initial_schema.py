"""initial schema

Revision ID: 20260416_0001
Revises:
Create Date: 2026-04-16 00:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260416_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


company_status_enum = sa.Enum(
    "new",
    "researching",
    "qualifying",
    "qualified",
    "rejected",
    "outreaching",
    "replied",
    "closed",
    name="company_status",
)

campaign_status_enum = sa.Enum(
    "active",
    "paused",
    "replied",
    "stopped",
    "completed",
    name="campaign_status",
)

message_direction_enum = sa.Enum(
    "outbound",
    "inbound",
    name="message_direction",
)


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_id", "tasks", ["id"], unique=False)
    op.create_index("ix_tasks_status", "tasks", ["status"], unique=False)

    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("industry", sa.String(length=255), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("status", company_status_enum, nullable=False),
        sa.Column("qualification_result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain"),
    )
    op.create_index("ix_companies_id", "companies", ["id"], unique=False)
    op.create_index("ix_companies_domain", "companies", ["domain"], unique=False)
    op.create_index("ix_companies_status", "companies", ["status"], unique=False)

    op.create_table(
        "blacklist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("value", sa.String(length=512), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("value"),
    )
    op.create_index("ix_blacklist_id", "blacklist", ["id"], unique=False)
    op.create_index("ix_blacklist_value", "blacklist", ["value"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_id", "audit_log", ["id"], unique=False)
    op.create_index("ix_audit_log_entity_type", "audit_log", ["entity_type"], unique=False)
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"], unique=False)
    op.create_index("ix_audit_log_action", "audit_log", ["action"], unique=False)

    op.create_table(
        "company_pages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("page_type", sa.String(length=64), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_company_pages_id", "company_pages", ["id"], unique=False)
    op.create_index("ix_company_pages_company_id", "company_pages", ["company_id"], unique=False)

    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=512), nullable=True),
        sa.Column("role", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contacts_id", "contacts", ["id"], unique=False)
    op.create_index("ix_contacts_company_id", "contacts", ["company_id"], unique=False)
    op.create_index("ix_contacts_email", "contacts", ["email"], unique=False)

    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("status", campaign_status_enum, nullable=False),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("brief", sa.Text(), nullable=True),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("has_reply", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaigns_id", "campaigns", ["id"], unique=False)
    op.create_index("ix_campaigns_company_id", "campaigns", ["company_id"], unique=False)
    op.create_index("ix_campaigns_contact_id", "campaigns", ["contact_id"], unique=False)
    op.create_index("ix_campaigns_status", "campaigns", ["status"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("direction", message_direction_enum, nullable=False),
        sa.Column("message_id", sa.String(length=1024), nullable=True),
        sa.Column("from_email", sa.String(length=320), nullable=True),
        sa.Column("to_email", sa.String(length=320), nullable=True),
        sa.Column("thread_reference", sa.String(length=2048), nullable=True),
        sa.Column("subject", sa.String(length=1024), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("step", sa.Integer(), nullable=True),
        sa.Column("has_attachments", sa.Boolean(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_id", "messages", ["id"], unique=False)
    op.create_index("ix_messages_campaign_id", "messages", ["campaign_id"], unique=False)
    op.create_index("ix_messages_message_id", "messages", ["message_id"], unique=False)
    op.create_index("ix_messages_from_email", "messages", ["from_email"], unique=False)
    op.create_index("ix_messages_to_email", "messages", ["to_email"], unique=False)

    op.create_table(
        "replies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("needs_human", sa.Boolean(), nullable=False),
        sa.Column("next_action", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_replies_id", "replies", ["id"], unique=False)
    op.create_index("ix_replies_message_id", "replies", ["message_id"], unique=False)
    op.create_index("ix_replies_label", "replies", ["label"], unique=False)

    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_name", sa.String(length=255), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("executed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedules_id", "schedules", ["id"], unique=False)
    op.create_index("ix_schedules_campaign_id", "schedules", ["campaign_id"], unique=False)
    op.create_index("ix_schedules_run_at", "schedules", ["run_at"], unique=False)

    op.create_table(
        "handoffs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("needs_human", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("recommended_reply", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_handoffs_id", "handoffs", ["id"], unique=False)
    op.create_index("ix_handoffs_campaign_id", "handoffs", ["campaign_id"], unique=False)
    op.create_index("ix_handoffs_company_id", "handoffs", ["company_id"], unique=False)
    op.create_index("ix_handoffs_contact_id", "handoffs", ["contact_id"], unique=False)
    op.create_index("ix_handoffs_label", "handoffs", ["label"], unique=False)
    op.create_index("ix_handoffs_priority", "handoffs", ["priority"], unique=False)
    op.create_index("ix_handoffs_status", "handoffs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_handoffs_status", table_name="handoffs")
    op.drop_index("ix_handoffs_priority", table_name="handoffs")
    op.drop_index("ix_handoffs_label", table_name="handoffs")
    op.drop_index("ix_handoffs_contact_id", table_name="handoffs")
    op.drop_index("ix_handoffs_company_id", table_name="handoffs")
    op.drop_index("ix_handoffs_campaign_id", table_name="handoffs")
    op.drop_index("ix_handoffs_id", table_name="handoffs")
    op.drop_table("handoffs")

    op.drop_index("ix_schedules_run_at", table_name="schedules")
    op.drop_index("ix_schedules_campaign_id", table_name="schedules")
    op.drop_index("ix_schedules_id", table_name="schedules")
    op.drop_table("schedules")

    op.drop_index("ix_replies_label", table_name="replies")
    op.drop_index("ix_replies_message_id", table_name="replies")
    op.drop_index("ix_replies_id", table_name="replies")
    op.drop_table("replies")

    op.drop_index("ix_messages_to_email", table_name="messages")
    op.drop_index("ix_messages_from_email", table_name="messages")
    op.drop_index("ix_messages_message_id", table_name="messages")
    op.drop_index("ix_messages_campaign_id", table_name="messages")
    op.drop_index("ix_messages_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_campaigns_status", table_name="campaigns")
    op.drop_index("ix_campaigns_contact_id", table_name="campaigns")
    op.drop_index("ix_campaigns_company_id", table_name="campaigns")
    op.drop_index("ix_campaigns_id", table_name="campaigns")
    op.drop_table("campaigns")

    op.drop_index("ix_contacts_email", table_name="contacts")
    op.drop_index("ix_contacts_company_id", table_name="contacts")
    op.drop_index("ix_contacts_id", table_name="contacts")
    op.drop_table("contacts")

    op.drop_index("ix_company_pages_company_id", table_name="company_pages")
    op.drop_index("ix_company_pages_id", table_name="company_pages")
    op.drop_table("company_pages")

    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_entity_id", table_name="audit_log")
    op.drop_index("ix_audit_log_entity_type", table_name="audit_log")
    op.drop_index("ix_audit_log_id", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("ix_blacklist_value", table_name="blacklist")
    op.drop_index("ix_blacklist_id", table_name="blacklist")
    op.drop_table("blacklist")

    op.drop_index("ix_companies_status", table_name="companies")
    op.drop_index("ix_companies_domain", table_name="companies")
    op.drop_index("ix_companies_id", table_name="companies")
    op.drop_table("companies")

    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_id", table_name="tasks")
    op.drop_table("tasks")


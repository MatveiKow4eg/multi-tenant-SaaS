from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), index=True)  # company, campaign, message...
    entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

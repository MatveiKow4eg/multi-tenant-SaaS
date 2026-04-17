import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CampaignStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    replied = "replied"
    stopped = "stopped"
    completed = "completed"


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.id"), index=True, nullable=False)
    contact_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("contacts.id"), index=True)
    status: Mapped[str] = mapped_column(
        Enum(CampaignStatus, name="campaign_status"),
        default=CampaignStatus.active,
        index=True,
    )
    language: Mapped[str | None] = mapped_column(String(10))
    brief: Mapped[str | None] = mapped_column(Text)
    step: Mapped[int] = mapped_column(Integer, default=0)  # 0=initial, 1=followup1, 2=followup2
    has_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company: Mapped["Company"] = relationship("Company", back_populates="campaigns")
    messages: Mapped[list["Message"]] = relationship("Message", back_populates="campaign")
    schedules: Mapped[list["Schedule"]] = relationship("Schedule", back_populates="campaign")


from app.models.company import Company  # noqa: E402, F401
from app.models.message import Message  # noqa: E402, F401
from app.models.schedule import Schedule  # noqa: E402, F401

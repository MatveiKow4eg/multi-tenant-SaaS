import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MessageDirection(str, enum.Enum):
    outbound = "outbound"
    inbound = "inbound"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    campaign_id: Mapped[int] = mapped_column(Integer, ForeignKey("campaigns.id"), index=True, nullable=False)
    direction: Mapped[str] = mapped_column(
        Enum(MessageDirection, name="message_direction"),
        nullable=False,
    )
    message_id: Mapped[str | None] = mapped_column(String(1024), index=True)  # SMTP/IMAP Message-ID header
    from_email: Mapped[str | None] = mapped_column(String(320), index=True)
    to_email: Mapped[str | None] = mapped_column(String(320), index=True)
    thread_reference: Mapped[str | None] = mapped_column(String(2048))
    subject: Mapped[str | None] = mapped_column(String(1024))
    body: Mapped[str | None] = mapped_column(Text)
    step: Mapped[int | None] = mapped_column(Integer)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="messages")
    reply: Mapped["Reply | None"] = relationship("Reply", back_populates="message", uselist=False)


from app.models.campaign import Campaign  # noqa: E402, F401
from app.models.reply import Reply  # noqa: E402, F401

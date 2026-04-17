from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Reply(Base):
    __tablename__ = "replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    message_id: Mapped[int] = mapped_column(Integer, ForeignKey("messages.id"), index=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String(64), index=True)  # interested / not_interested / ...
    summary: Mapped[str | None] = mapped_column(Text)
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False)
    next_action: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped["Message"] = relationship("Message", back_populates="reply")


from app.models.message import Message  # noqa: E402, F401

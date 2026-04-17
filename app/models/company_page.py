from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CompanyPage(Base):
    __tablename__ = "company_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.id"), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    page_type: Mapped[str | None] = mapped_column(String(64))  # homepage, about, contact, careers...
    raw_text: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    company: Mapped["Company"] = relationship("Company", back_populates="pages")


# avoid circular import
from app.models.company import Company  # noqa: E402, F401

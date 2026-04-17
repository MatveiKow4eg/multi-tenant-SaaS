import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CompanyStatus(str, enum.Enum):
    new = "new"
    researching = "researching"
    qualifying = "qualifying"
    qualified = "qualified"
    rejected = "rejected"
    outreaching = "outreaching"
    replied = "replied"
    closed = "closed"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tenants.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(512))
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    country: Mapped[str | None] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(255))
    score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        Enum(CompanyStatus, name="company_status"),
        default=CompanyStatus.new,
        index=True,
    )
    qualification_result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    pages: Mapped[list["CompanyPage"]] = relationship("CompanyPage", back_populates="company")
    contacts: Mapped[list["Contact"]] = relationship("Contact", back_populates="company")
    campaigns: Mapped[list["Campaign"]] = relationship("Campaign", back_populates="company")
    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="companies")


from app.models.tenant import Tenant  # noqa: E402, F401

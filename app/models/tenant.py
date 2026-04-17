from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    companies: Mapped[list["Company"]] = relationship("Company", back_populates="tenant")
    memberships: Mapped[list["TenantMembership"]] = relationship("TenantMembership", back_populates="tenant")


from app.models.company import Company  # noqa: E402, F401
from app.models.tenant_membership import TenantMembership  # noqa: E402, F401

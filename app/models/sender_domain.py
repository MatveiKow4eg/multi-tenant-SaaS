from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class SenderDomain(Base):
    """Sender domain with DNS verification state."""

    __tablename__ = "sender_domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # ownership verification status: pending / verified / failed
    ownership_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    ownership_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ownership_verified_via: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ownership_email_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    ownership_dns_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    ownership_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ownership_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ownership_host: Mapped[str] = mapped_column(String(255), nullable=False, default="_lertisento-verify")
    ownership_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ownership_email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ownership_dns_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ownership_last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # authentication status: pending / verified / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # per-record statuses
    spf_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    dkim_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    dmarc_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # dkim_mode: cname (managed selectors) or txt (fallback)
    dkim_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="cname")
    # send_enabled: true only when fully verified
    send_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    from_local_part: Mapped[str] = mapped_column(String(128), nullable=False, default="hello")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant")
    dns_records: Mapped[list["SenderDomainDnsRecord"]] = relationship(
        "SenderDomainDnsRecord", back_populates="sender_domain", cascade="all, delete-orphan"
    )
    dkim_keys: Mapped[list["DkimKeyPair"]] = relationship(
        "DkimKeyPair", back_populates="sender_domain", cascade="all, delete-orphan"
    )
    managed_selectors: Mapped[list["ManagedDkimSelector"]] = relationship(
        "ManagedDkimSelector", back_populates="sender_domain", cascade="all, delete-orphan"
    )

    @property
    def send_from_email(self) -> str:
        return f"{self.from_local_part}@{self.domain}"


class SenderDomainDnsRecord(Base):
    """Individual DNS record that must be configured by the user."""

    __tablename__ = "sender_domain_dns_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sender_domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sender_domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)    # spf | dkim | dmarc
    record_type: Mapped[str] = mapped_column(String(8), nullable=False)  # TXT | CNAME
    host: Mapped[str] = mapped_column(String(255), nullable=False)       # @ or selector._domainkey
    value: Mapped[str] = mapped_column(Text, nullable=False)             # expected value shown to user
    selector: Mapped[str | None] = mapped_column(String(64), nullable=True)  # DKIM selector if applicable
    # verification state
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    actual_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sender_domain: Mapped["SenderDomain"] = relationship("SenderDomain", back_populates="dns_records")


class DkimKeyPair(Base):
    """RSA key pair for DKIM signing. Private key stored encrypted."""

    __tablename__ = "dkim_key_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sender_domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sender_domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    selector: Mapped[str] = mapped_column(String(64), nullable=False)
    private_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)   # base64 DER
    algorithm: Mapped[str] = mapped_column(String(8), nullable=False, default="rsa2048")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sender_domain: Mapped["SenderDomain"] = relationship("SenderDomain", back_populates="dkim_keys")


class ManagedDkimSelector(Base):
    """Maps a public selector host on the customer's domain to our managed target."""

    __tablename__ = "managed_dkim_selectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sender_domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sender_domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dkim_key_pair_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("dkim_key_pairs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    selector: Mapped[str] = mapped_column(String(64), nullable=False)
    cname_target: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sender_domain: Mapped["SenderDomain"] = relationship("SenderDomain", back_populates="managed_selectors")
    dkim_key_pair: Mapped["DkimKeyPair | None"] = relationship("DkimKeyPair")


from app.models.tenant import Tenant  # noqa: E402, F401

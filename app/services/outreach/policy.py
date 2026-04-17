from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.blacklist import Blacklist

COUNTRY_LANGUAGE_MAP: dict[str, str] = {
    "lithuania": "lt",
    "estonia": "et",
}

PARTNER_REASON_KEYWORDS: tuple[str, ...] = (
    "partner",
    "existing partner",
    "already partner",
)


def normalize_country(country: str | None) -> str:
    return (country or "").strip().lower()


def get_allowed_countries() -> set[str]:
    raw = settings.outreach_allowed_countries or "Lithuania"
    return {normalize_country(item) for item in raw.split(",") if item.strip()}


def is_country_allowed_for_outreach(country: str | None) -> bool:
    normalized = normalize_country(country)
    if not normalized:
        return False
    return normalized in get_allowed_countries()


def language_for_country(country: str | None) -> str:
    normalized = normalize_country(country)
    return COUNTRY_LANGUAGE_MAP.get(normalized, "en")


def get_blacklist_skip_reason(
    *,
    company_domain: str,
    contact_email: str | None,
    db: Session,
) -> str | None:
    domain_hit = (
        db.query(Blacklist)
        .filter(
            Blacklist.entry_type == "domain",
            func.lower(Blacklist.value) == company_domain.lower(),
        )
        .first()
    )
    if domain_hit:
        reason = (domain_hit.reason or "").strip()
        if _is_partner_reason(reason):
            return "existing_partner"
        return "blacklisted_domain"

    if contact_email:
        email_hit = (
            db.query(Blacklist)
            .filter(
                Blacklist.entry_type == "email",
                func.lower(Blacklist.value) == contact_email.lower(),
            )
            .first()
        )
        if email_hit:
            reason = (email_hit.reason or "").strip()
            if _is_partner_reason(reason):
                return "existing_partner"
            return "blacklisted_email"

    return None


def _is_partner_reason(reason: str) -> bool:
    low = reason.lower()
    return any(keyword in low for keyword in PARTNER_REASON_KEYWORDS)

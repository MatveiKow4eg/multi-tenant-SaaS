from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.company_page import CompanyPage
from app.models.contact import Contact

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r'mailto:([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})', re.IGNORECASE)

ROLE_HINTS: list[tuple[str, str, float]] = [
    ("hr", "hr", 0.95),
    ("recruit", "recruitment", 0.95),
    ("career", "careers", 0.9),
    ("karjer", "careers", 0.9),      # Lithuanian: karjera
    ("darbas", "careers", 0.9),      # Lithuanian: jobs
    ("operations", "operations", 0.8),
    ("procurement", "procurement", 0.8),
    ("pirkimai", "procurement", 0.8), # Lithuanian: purchases
    ("sales", "sales", 0.75),
    ("pardavimai", "sales", 0.75),    # Lithuanian: sales
    ("director", "management", 0.75),
    ("vadovas", "management", 0.75),  # Lithuanian: manager/director
    ("info@", "general", 0.5),
    ("info", "general", 0.45),
    ("kontakt", "general", 0.5),
    ("office@", "general", 0.55),
    ("hello@", "general", 0.5),
]


@dataclass
class ResolvedContact:
    email: str
    role: str
    confidence: float
    source_url: str | None


def _score_email(email: str, context_text: str) -> tuple[str, float]:
    low_email = email.lower()
    low_ctx = context_text.lower()
    for marker, role, score in ROLE_HINTS:
        if marker in low_email or marker in low_ctx:
            return role, score
    return "unknown", 0.4


def _extract_emails_from_text(text: str) -> list[str]:
    """Extract emails from both plain text and mailto: HTML attributes."""
    found: list[str] = []
    found.extend(EMAIL_RE.findall(text))
    # mailto: links may survive even after HTML stripping partially
    found.extend(MAILTO_RE.findall(text))
    return found


def resolve_contacts_for_company(company_id: int, db: Session) -> list[ResolvedContact]:
    pages = db.query(CompanyPage).filter(CompanyPage.company_id == company_id).all()
    seen: set[str] = set()
    output: list[ResolvedContact] = []

    for page in pages:
        text = page.raw_text or ""
        emails = _extract_emails_from_text(text)
        for email in emails:
            low_email = email.lower().strip(".,;:!?)\"'")
            # skip obviously invalid / image / asset emails
            if not low_email or "." not in low_email.split("@")[-1]:
                continue
            if any(low_email.endswith(ext) for ext in (".png", ".jpg", ".gif", ".svg", ".css", ".js")):
                continue
            if low_email in seen:
                continue
            seen.add(low_email)
            role, confidence = _score_email(low_email, text[:2000])
            output.append(
                ResolvedContact(
                    email=low_email,
                    role=role,
                    confidence=confidence,
                    source_url=page.url,
                )
            )

    output.sort(key=lambda x: x.confidence, reverse=True)
    return output


def save_contacts(company_id: int, contacts: list[ResolvedContact], db: Session) -> list[Contact]:
    created: list[Contact] = []
    existing_emails = {
        x.email.lower()
        for x in db.query(Contact).filter(Contact.company_id == company_id).all()
    }

    for c in contacts:
        if c.email in existing_emails:
            continue
        row = Contact(
            company_id=company_id,
            email=c.email,
            role=c.role,
            source_url=c.source_url,
            confidence=c.confidence,
        )
        db.add(row)
        created.append(row)
        existing_emails.add(c.email)

    db.flush()
    return created

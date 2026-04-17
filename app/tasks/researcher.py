from __future__ import annotations

from app.db.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.company import Company, CompanyStatus
from app.models.company_page import CompanyPage
from app.services.contact_resolver.resolver import resolve_contacts_for_company, save_contacts
from app.services.outreach.policy import is_country_allowed_for_outreach, normalize_country
from app.services.qualifier.openai_qualifier import qualify_company
from app.services.researcher.site_researcher import research_company_site
from app.worker.celery_app import celery_app


@celery_app.task(name="researcher.run_for_company", bind=True, max_retries=2)
def run_research_and_qualify(self, company_id: int, tenant_id: int | None = None) -> dict:
    db = SessionLocal()
    try:
        q = db.query(Company).filter(Company.id == company_id)
        if tenant_id is not None:
            q = q.filter(Company.tenant_id == tenant_id)
        company = q.first()
        if not company:
            return {"ok": False, "error": "company_not_found", "company_id": company_id}

        company.status = CompanyStatus.researching
        db.add(company)
        db.flush()

        research = research_company_site(company.domain)

        for p in research.pages:
            db.add(
                CompanyPage(
                    company_id=company.id,
                    url=p.url,
                    page_type=p.page_type,
                    raw_text=p.text,
                )
            )

        db.add(
            AuditLog(
                entity_type="company",
                entity_id=company.id,
                action="site_researched",
                details={
                    "pages_found": len(research.pages),
                    "languages_found": research.languages_found,
                    "has_careers_page": research.has_careers_page,
                },
                reason="Researcher module completed website crawl.",
            )
        )

        company.status = CompanyStatus.qualifying
        qualification = qualify_company(research)

        company.qualification_result = qualification
        company.score = float(qualification.get("score", 0))
        company.industry = qualification.get("industry") or company.industry
        company.country = (
            qualification.get("country") if qualification.get("country") not in {None, "unknown"} else company.country
        )

        country_allowed = is_country_allowed_for_outreach(company.country)
        if qualification.get("is_relevant") and country_allowed:
            company.status = CompanyStatus.qualified
        elif qualification.get("is_relevant") and not country_allowed:
            qualification.setdefault("signals", []).append("country_not_allowed")
            qualification["reason"] = (
                f"Company country '{company.country}' is not in allowed outreach countries."
            )
            company.qualification_result = qualification
            company.status = CompanyStatus.rejected
            db.add(
                AuditLog(
                    entity_type="company",
                    entity_id=company.id,
                    action="qualified_but_skipped_country",
                    details={
                        "country": company.country,
                        "normalized_country": normalize_country(company.country),
                        "reason": "country_not_allowed",
                    },
                    reason="Skipped because country not allowed.",
                )
            )
        else:
            company.status = CompanyStatus.rejected

        contacts_created = []
        if qualification.get("is_relevant") and country_allowed:
            resolved_contacts = resolve_contacts_for_company(company.id, db)
            contacts_created = save_contacts(company.id, resolved_contacts, db)

        db.add(
            AuditLog(
                entity_type="company",
                entity_id=company.id,
                action="company_qualified",
                details=qualification,
                reason="Qualifier module completed company assessment.",
            )
        )
        if contacts_created:
            db.add(
                AuditLog(
                    entity_type="company",
                    entity_id=company.id,
                    action="contacts_resolved",
                    details={
                        "contacts_created": len(contacts_created),
                        "top_contacts": [
                            {
                                "email": c.email,
                                "role": c.role,
                                "confidence": c.confidence,
                            }
                            for c in contacts_created[:5]
                        ],
                    },
                    reason="Contact Resolver extracted contacts from company pages.",
                )
            )

        db.commit()

        return {
            "ok": True,
            "company_id": company.id,
            "domain": company.domain,
            "pages_found": len(research.pages),
            "contacts_created": len(contacts_created),
            "qualification": qualification,
        }
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=90)
    finally:
        db.close()

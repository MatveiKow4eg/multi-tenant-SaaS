from __future__ import annotations

from app.db.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.company import Company, CompanyStatus
from app.models.contact import Contact
from app.models.message import Message, MessageDirection
from app.services.outreach.policy import get_blacklist_skip_reason, is_country_allowed_for_outreach, language_for_country
from app.services.outreach.writer import generate_outreach_sequence
from app.worker.celery_app import celery_app


@celery_app.task(name="outreach.generate_for_company", bind=True, max_retries=2)
def generate_outreach_for_company(self, company_id: int, tenant_id: int | None = None) -> dict:
    db = SessionLocal()
    try:
        import logging

        logger = logging.getLogger("outreach.task")
        q = db.query(Company).filter(Company.id == company_id)
        if tenant_id is not None:
            q = q.filter(Company.tenant_id == tenant_id)
        company = q.first()
        if not company:
            return {"ok": False, "error": "company_not_found", "company_id": company_id}

        if not is_country_allowed_for_outreach(company.country):
            db.add(
                AuditLog(
                    entity_type="company",
                    entity_id=company.id,
                    action="outreach_skipped",
                    details={"reason": "country_not_allowed", "country": company.country},
                    reason="Skipped because country not allowed for outreach.",
                )
            )
            db.commit()
            logger.info("Outreach skipped because country not allowed: company_id=%s country=%s", company.id, company.country)
            return {"ok": False, "error": "country_not_allowed", "company_id": company_id}

        if company.status not in {CompanyStatus.qualified, CompanyStatus.outreaching}:
            return {
                "ok": False,
                "error": "company_not_qualified",
                "company_id": company_id,
                "status": str(company.status),
            }

        contact = (
            db.query(Contact)
            .filter(Contact.company_id == company_id)
            .order_by(Contact.confidence.desc().nullslast(), Contact.id.asc())
            .first()
        )
        if not contact:
            return {"ok": False, "error": "contact_not_found", "company_id": company_id}

        skip_reason = get_blacklist_skip_reason(
            company_domain=company.domain,
            contact_email=contact.email,
            db=db,
        )
        if skip_reason:
            db.add(
                AuditLog(
                    entity_type="company",
                    entity_id=company.id,
                    action="outreach_skipped",
                    details={"reason": skip_reason, "domain": company.domain, "email": contact.email},
                    reason="Skipped because blacklisted before campaign generation.",
                )
            )
            db.commit()
            logger.info("Outreach skipped because blacklisted: company_id=%s reason=%s", company.id, skip_reason)
            return {"ok": False, "error": skip_reason, "company_id": company_id}

        active = (
            db.query(Campaign)
            .filter(
                Campaign.company_id == company_id,
                Campaign.status.in_([CampaignStatus.active, CampaignStatus.paused]),
            )
            .first()
        )
        if active:
            return {
                "ok": False,
                "error": "active_campaign_exists",
                "company_id": company_id,
                "campaign_id": active.id,
            }

        qualification = company.qualification_result or {}
        language = language_for_country(company.country)
        brief = (
            f"Company {company.name or company.domain} ({company.domain}), "
            f"industry={company.industry or 'unknown'}, score={company.score}, "
            f"signals={qualification.get('signals', [])}"
        )

        sequence = generate_outreach_sequence(
            company_name=company.name or company.domain,
            industry=company.industry or "manufacturing",
            country=company.country or "unknown",
            recipient_role=contact.role or "unknown",
            language=language,
            brief=brief,
        )

        campaign = Campaign(
            company_id=company_id,
            contact_id=contact.id,
            status=CampaignStatus.active,
            language=language,
            brief=brief,
            step=0,
            has_reply=False,
        )
        db.add(campaign)
        db.flush()

        db.add(
            Message(
                campaign_id=campaign.id,
                direction=MessageDirection.outbound,
                from_email=None,
                to_email=contact.email,
                subject=sequence.get("subject", "Partnership inquiry"),
                body=sequence.get("body_step_1", ""),
                step=0,
            )
        )
        db.add(
            Message(
                campaign_id=campaign.id,
                direction=MessageDirection.outbound,
                from_email=None,
                to_email=contact.email,
                subject=sequence.get("subject", "Partnership inquiry"),
                body=sequence.get("body_followup_1", ""),
                step=1,
            )
        )
        db.add(
            Message(
                campaign_id=campaign.id,
                direction=MessageDirection.outbound,
                from_email=None,
                to_email=contact.email,
                subject=sequence.get("subject", "Partnership inquiry"),
                body=sequence.get("body_followup_2", ""),
                step=2,
            )
        )

        company.status = CompanyStatus.outreaching

        db.add(
            AuditLog(
                entity_type="campaign",
                entity_id=campaign.id,
                action="outreach_sequence_generated",
                details={
                    "company_id": company_id,
                    "contact_id": contact.id,
                    "language": language,
                    "subject": sequence.get("subject", ""),
                },
                reason="Outreach Writer created initial sequence drafts.",
            )
        )

        logger.info(
            "Generated outreach: company_id=%s language=%s country=%s",
            company.id,
            language,
            company.country,
        )

        db.commit()
        return {"ok": True, "company_id": company_id, "campaign_id": campaign.id}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=90)
    finally:
        db.close()

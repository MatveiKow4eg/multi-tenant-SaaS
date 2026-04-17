from __future__ import annotations

import uuid

from redis import Redis

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.company import Company, CompanyStatus
from app.models.company_page import CompanyPage
from app.models.contact import Contact
from app.services.contact_resolver.resolver import resolve_contacts_for_company, save_contacts
from app.services.outreach.policy import (
    get_blacklist_skip_reason,
    is_country_allowed_for_outreach,
    language_for_country,
)
from app.services.outreach.writer import generate_outreach_sequence
from app.services.qualifier.openai_qualifier import qualify_company
from app.services.researcher.site_researcher import ResearchResult, PageSnapshot, research_company_site
from app.tasks.researcher import run_research_and_qualify
from app.worker.celery_app import celery_app

# ─── Redis helpers ────────────────────────────────────────────────────────────

_JOB_TTL = 3600  # seconds


def _redis() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def _job_key(job_id: str) -> str:
    return f"prepare_all:{job_id}"


def init_job(job_id: str, total: int) -> None:
    r = _redis()
    key = _job_key(job_id)
    r.hset(key, mapping={"total": total, "done": 0, "errors": 0, "status": "running"})
    r.expire(key, _JOB_TTL)


def get_job_progress(job_id: str) -> dict | None:
    r = _redis()
    data = r.hgetall(_job_key(job_id))
    if not data:
        return None
    total = int(data.get("total", 0))
    done = int(data.get("done", 0))
    errors = int(data.get("errors", 0))
    pct = round(done / total * 100) if total > 0 else 0
    return {
        "job_id": job_id,
        "total": total,
        "done": done,
        "errors": errors,
        "pct": pct,
        "status": data.get("status", "unknown"),
    }


def _inc(job_id: str, field: str) -> None:
    r = _redis()
    key = _job_key(job_id)
    r.hincrby(key, field, 1)
    # Check if done
    data = r.hgetall(key)
    if data and int(data.get("done", 0)) + int(data.get("errors", 0)) >= int(data.get("total", 0)):
        r.hset(key, "status", "done")


# ─── Single-company full pipeline task ───────────────────────────────────────


def _research_result_from_pages(domain: str, pages: list) -> ResearchResult:
    snapshots = [
        PageSnapshot(url=p.url, page_type=p.page_type or "page", text=p.raw_text or "")
        for p in pages
    ]
    text_chunks = [p.text for p in snapshots if p.text]
    joined = "\n".join(text_chunks)[:5000]
    return ResearchResult(
        domain=domain,
        pages=snapshots,
        languages_found=[],
        has_careers_page=any(p.page_type in ("careers", "jobs") for p in snapshots),
        text_summary=joined,
    )


@celery_app.task(name="pipeline.prepare_company_full", bind=True, max_retries=1)
def prepare_company_full(self, company_id: int, job_id: str, tenant_id: int | None = None) -> dict:
    db = SessionLocal()
    try:
        q = db.query(Company).filter(Company.id == company_id)
        if tenant_id is not None:
            q = q.filter(Company.tenant_id == tenant_id)
        company = q.first()
        if not company:
            _inc(job_id, "errors")
            return {"ok": False, "error": "not_found", "company_id": company_id}

        pages = db.query(CompanyPage).filter(CompanyPage.company_id == company_id).all()
        contacts_exist = db.query(Contact).filter(Contact.company_id == company_id).count() > 0
        research_result: ResearchResult | None = None

        # Step 1: Research
        # Re-scrape if: no pages at all, OR has pages but no contacts yet
        # (old scraper missed Baltic contact pages — re-scrape to find /kontaktai etc.)
        need_rescrape = not pages or (not contacts_exist)
        if need_rescrape:
            research_result = research_company_site(company.domain)
            if not pages:
                # Fresh scrape — save all pages
                for p in research_result.pages:
                    db.add(CompanyPage(company_id=company.id, url=p.url, page_type=p.page_type, raw_text=p.text))
            else:
                # Incremental — only save pages with URLs not yet stored
                existing_urls = {p.url for p in pages}
                new_pages = [p for p in research_result.pages if p.url not in existing_urls]
                for p in new_pages:
                    db.add(CompanyPage(company_id=company.id, url=p.url, page_type=p.page_type, raw_text=p.text))
            db.add(AuditLog(
                entity_type="company", entity_id=company.id, action="site_researched",
                details={"pages_found": len(research_result.pages), "source": "prepare_all"},
                reason="prepare_all pipeline",
            ))
            db.flush()
            pages = db.query(CompanyPage).filter(CompanyPage.company_id == company_id).all()

        # Step 2: Qualify
        if not company.qualification_result:
            if research_result is None:
                research_result = _research_result_from_pages(company.domain, pages)
            qualification = qualify_company(research_result)
            company.qualification_result = qualification
            company.score = float(qualification.get("score", 0))
            company.industry = qualification.get("industry") or company.industry
            inferred_country = qualification.get("country")
            if inferred_country not in {None, "unknown"}:
                company.country = inferred_country
            country_allowed = is_country_allowed_for_outreach(company.country)
            company.status = (
                CompanyStatus.qualified if qualification.get("is_relevant") and country_allowed
                else CompanyStatus.rejected
            )
            db.add(AuditLog(
                entity_type="company", entity_id=company.id, action="company_qualified",
                details={**qualification, "source": "prepare_all"},
                reason="prepare_all pipeline",
            ))

        # Step 3: Contacts
        contacts = db.query(Contact).filter(Contact.company_id == company_id).all()
        if not contacts:
            resolved = resolve_contacts_for_company(company.id, db)
            created = save_contacts(company.id, resolved, db)
            contacts = db.query(Contact).filter(Contact.company_id == company_id).all()
            db.add(AuditLog(
                entity_type="company", entity_id=company.id, action="contacts_resolved",
                details={"created": len(created), "source": "prepare_all"},
                reason="prepare_all pipeline",
            ))

        # Step 4: Outreach draft
        existing_draft = (
            db.query(AuditLog)
            .filter(
                AuditLog.entity_type == "company",
                AuditLog.entity_id == company_id,
                AuditLog.action == "ui_outreach_draft_generated",
            )
            .first()
        )
        if not existing_draft and contacts and is_country_allowed_for_outreach(company.country):
            top_contact = sorted(contacts, key=lambda c: c.confidence or 0, reverse=True)[0]
            skip_reason = get_blacklist_skip_reason(
                company_domain=company.domain,
                contact_email=top_contact.email,
                db=db,
            )
            if not skip_reason:
                qdata = company.qualification_result or {}
                brief = (
                    f"Company {company.name or company.domain} ({company.domain}), "
                    f"industry={company.industry or 'unknown'}, score={company.score}, "
                    f"signals={qdata.get('signals', [])}"
                )
                language = language_for_country(company.country)
                sequence = generate_outreach_sequence(
                    company_name=company.name or company.domain,
                    industry=company.industry or "manufacturing",
                    country=company.country or "unknown",
                    recipient_role=top_contact.role or "unknown",
                    language=language,
                    brief=brief,
                )
                db.add(AuditLog(
                    entity_type="company", entity_id=company.id,
                    action="ui_outreach_draft_generated",
                    details={
                        "contact_id": top_contact.id,
                        "contact_email": top_contact.email,
                        "language": language,
                        "subject": sequence.get("subject"),
                        "body_step_1": sequence.get("body_step_1"),
                        "body_followup_1": sequence.get("body_followup_1"),
                        "body_followup_2": sequence.get("body_followup_2"),
                        "source": "prepare_all",
                    },
                    reason="Outreach draft from prepare_all pipeline.",
                ))

        db.add(company)
        db.commit()
        _inc(job_id, "done")
        return {"ok": True, "company_id": company_id}

    except Exception as exc:
        db.rollback()
        _inc(job_id, "errors")
        raise self.retry(exc=exc, countdown=10)
    finally:
        db.close()


# ─── Batch launcher ───────────────────────────────────────────────────────────


@celery_app.task(name="pipeline.research_batch")
def run_research_batch(company_ids: list[int], tenant_id: int | None = None) -> dict:
    for company_id in company_ids:
        run_research_and_qualify.delay(company_id=company_id, tenant_id=tenant_id)
    return {"scheduled": len(company_ids)}


def launch_prepare_all(company_ids: list[int], tenant_id: int | None = None) -> str:
    """Dispatch prepare_company_full for each company; return job_id."""
    job_id = str(uuid.uuid4())
    init_job(job_id, len(company_ids))
    for cid in company_ids:
        prepare_company_full.delay(company_id=cid, job_id=job_id, tenant_id=tenant_id)
    return job_id


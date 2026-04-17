"""
Сервис сохранения результатов Finder в базу данных.
Дедупликация происходит на трёх уровнях:
  1. in-memory (set[domain]) внутри одного запуска finder.find_companies()
  2. уникальный индекс companies.domain в PostgreSQL
  3. проверка перед INSERT (чтобы избежать IntegrityError в логах)
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.blacklist import Blacklist
from app.models.company import Company
from app.services.finder.finder import FinderResult


def domain_in_blacklist(domain: str, db: Session) -> bool:
    return (
        db.query(Blacklist)
        .filter(Blacklist.value == domain, Blacklist.entry_type == "domain")
        .first()
    ) is not None


def domain_exists(domain: str, db: Session, tenant_id: int | None = None) -> bool:
    q = db.query(Company).filter(Company.domain == domain)
    if tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    return q.first() is not None


def save_finder_results(results: list[FinderResult], db: Session, tenant_id: int | None = None) -> list[Company]:
    """
    Persist new companies found by Finder.
    Returns list of newly created Company records.
    """
    import logging
    logger = logging.getLogger("finder")
    
    created: list[Company] = []
    skipped_blacklist = 0
    skipped_duplicate = 0
    
    for r in results:
        if domain_in_blacklist(r.domain, db):
            skipped_blacklist += 1
            continue
        if domain_exists(r.domain, db, tenant_id=tenant_id):
            skipped_duplicate += 1
            logger.debug(f"Finder: domain {r.domain} already exists, skipping")
            continue

        company = Company(
            tenant_id=tenant_id,
            domain=r.domain,
            name=r.title or None,
            country=r.country,
            industry=r.keyword,
        )
        db.add(company)
        db.flush()  # get id before commit

        db.add(
            AuditLog(
                entity_type="company",
                entity_id=company.id,
                action="domain_found",
                details={"url": r.url, "keyword": r.keyword, "snippet": r.snippet},
                reason=f"Finder: keyword='{r.keyword}', country='{r.country}'",
            )
        )
        created.append(company)
        logger.debug(f"Finder: saved new company {r.domain}")

    db.commit()
    logger.info(f"Finder: results saved: {len(created)} new, {skipped_duplicate} duplicates, {skipped_blacklist} blacklisted")
    return created

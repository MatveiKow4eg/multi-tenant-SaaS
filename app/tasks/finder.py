"""
Celery task: запускает Finder, сохраняет найденные компании в БД.
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.services.finder.finder import find_companies, find_companies_by_plan
from app.services.finder.storage import save_finder_results
from app.worker.celery_app import celery_app


@celery_app.task(name="finder.run", bind=True, max_retries=2)
def run_finder(
    self,
    countries: list[str],
    keywords: list[str] | None = None,
    results_per_query: int = 10,
    tenant_id: int | None = None,
) -> dict:
    db = SessionLocal()
    try:
        import logging
        logger = logging.getLogger("finder")
        
        # collect already-known domains for in-memory dedup
        from app.models.company import Company
        # FIXED: with_entities returns scalar values, not objects
        known_query = db.query(Company).with_entities(Company.domain)
        if tenant_id is not None:
            known_query = known_query.filter(Company.tenant_id == tenant_id)
        known: set[str] = {row for (row,) in known_query.all()}
        logger.info(f"Finder.run: starting with {len(known)} known domains")

        results = find_companies(
            countries=countries,
            keywords=keywords,
            results_per_query=results_per_query,
            known_domains=known,
        )
        logger.info(f"Finder.run: found {len(results)} potential companies")

        created = save_finder_results(results, db, tenant_id=tenant_id)
        logger.info(f"Finder.run: saved {len(created)} new companies: {[c.domain for c in created]}")
        
        return {
            "found": len(results),
            "saved": len(created),
            "domains": [c.domain for c in created],
        }
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=60)
    finally:
        db.close()


@celery_app.task(name="finder.run_with_plan", bind=True, max_retries=2)
def run_finder_with_plan(
    self,
    intent: str,
    country: str = "Lithuania",
    results_per_query: int = 20,
    tenant_id: int | None = None,
) -> dict:
    """Celery task: generate AI search plan then run Finder by that plan."""
    import logging
    logger = logging.getLogger("finder")

    db = SessionLocal()
    try:
        from app.models.company import Company
        from app.services.finder.search_planner import generate_search_plan

        known_query = db.query(Company).with_entities(Company.domain)
        if tenant_id is not None:
            known_query = known_query.filter(Company.tenant_id == tenant_id)
        known: set[str] = {row for (row,) in known_query.all()}
        logger.info("Finder[plan].run: known=%d, generating plan for country='%s'", len(known), country)

        try:
            plan = generate_search_plan(intent, country=country)
        except ValueError as exc:
            logger.error("Finder[plan].run: invalid country guardrail: %s", exc)
            return {"error": str(exc)}

        logger.info(
            "Finder[plan].run: plan ready — en=%d lt=%d industries=%d",
            len(plan.search_queries_en),
            len(plan.search_queries_lt),
            len(plan.priority_industries),
        )

        results = find_companies_by_plan(
            plan=plan,
            results_per_query=results_per_query,
            known_domains=known,
        )

        created = save_finder_results(results, db, tenant_id=tenant_id)
        logger.info(
            "Finder[plan].run: found=%d saved=%d",
            len(results),
            len(created),
        )

        return {
            "found": len(results),
            "saved": len(created),
            "domains": [c.domain for c in created],
            "plan_summary": {
                "country": plan.country,
                "en_queries": len(plan.search_queries_en),
                "lt_queries": len(plan.search_queries_lt),
                "industries": plan.priority_industries,
                "total_queries_executed": len(plan.all_queries()),
            },
        }
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=60)
    finally:
        db.close()

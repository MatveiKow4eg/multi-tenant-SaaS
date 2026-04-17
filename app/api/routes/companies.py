from fastapi import APIRouter, Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.api.security import require_roles
from app.db.session import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.schemas.company import (
    CompanyDetailRead,
    CompanyRead,
    ContactRead,
    FinderRunRequest,
    FinderRunResponse,
    OutreachRunResponse,
    ResearchBatchRequest,
    ResearchRunResponse,
    SearchPlannerRunRequest,
    SearchPlannerRunResponse,
)
from app.tasks.outreach import generate_outreach_for_company
from app.tasks.finder import run_finder, run_finder_with_plan
from app.tasks.pipeline import run_research_batch
from app.tasks.researcher import run_research_and_qualify

router = APIRouter()


@router.get("/", response_model=list[CompanyRead])
def list_companies(
    status: str | None = None,
    country: str | None = None,
    limit: int = 100,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> list[Company]:
    q = db.query(Company)
    if tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    if status:
        q = q.filter(Company.status == status)
    if country:
        q = q.filter(Company.country == country)
    return q.order_by(Company.created_at.desc()).limit(limit).all()


@router.get("/{company_id}", response_model=CompanyDetailRead)
def get_company(
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> Company:
    q = db.query(Company).filter(Company.id == company_id)
    if tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    company = q.first()
    if not company:
        raise HTTPException(status_code=404, detail="company_not_found")
    return company


@router.get("/{company_id}/contacts", response_model=list[ContactRead])
def get_company_contacts(
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> list[Contact]:
    q = db.query(Contact).join(Company, Company.id == Contact.company_id).filter(Contact.company_id == company_id)
    if tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    return q.order_by(Contact.confidence.desc().nullslast(), Contact.id.asc()).all()


@router.post("/finder/run", response_model=FinderRunResponse, status_code=202)
def trigger_finder(
    payload: FinderRunRequest,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
) -> FinderRunResponse:
    task = run_finder.delay(
        countries=payload.countries,
        keywords=payload.keywords,
        results_per_query=payload.results_per_query,
        tenant_id=tenant_id,
    )
    return FinderRunResponse(task_id=task.id)


@router.post("/{company_id}/research-qualify", response_model=ResearchRunResponse, status_code=202)
def trigger_research_qualify(
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> ResearchRunResponse:
    q = db.query(Company.id).filter(Company.id == company_id)
    if tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    exists = q.first()
    if not exists:
        raise HTTPException(status_code=404, detail="company_not_found")
    task = run_research_and_qualify.delay(company_id=company_id, tenant_id=tenant_id)
    return ResearchRunResponse(task_id=task.id)


@router.post("/research-qualify/batch", response_model=ResearchRunResponse, status_code=202)
def trigger_research_batch(
    payload: ResearchBatchRequest,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> ResearchRunResponse:
    q = db.query(Company.id).filter(Company.id.in_(payload.company_ids))
    if tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    found_ids = {row[0] for row in q.all()}
    if len(found_ids) != len(set(payload.company_ids)):
        raise HTTPException(status_code=404, detail="company_not_found")

    task = run_research_batch.delay(company_ids=payload.company_ids, tenant_id=tenant_id)
    return ResearchRunResponse(task_id=task.id)


@router.post("/{company_id}/outreach/generate", response_model=OutreachRunResponse, status_code=202)
def trigger_outreach_generate(
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> OutreachRunResponse:
    q = db.query(Company.id).filter(Company.id == company_id)
    if tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    exists = q.first()
    if not exists:
        raise HTTPException(status_code=404, detail="company_not_found")
    task = generate_outreach_for_company.delay(company_id=company_id, tenant_id=tenant_id)
    return OutreachRunResponse(task_id=task.id)


@router.post("/finder/plan-and-run", response_model=SearchPlannerRunResponse, status_code=202)
def trigger_plan_and_run(
    payload: SearchPlannerRunRequest,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
) -> SearchPlannerRunResponse:
    """
    Generate an AI search plan from a free-text intent and launch the Finder.

    Currently restricted to Lithuania. Returns task_id and a preview of the generated plan.
    """
    from app.services.finder.search_planner import generate_search_plan

    # Generate the plan synchronously here so the caller sees the plan_summary immediately.
    # The actual search runs in the background Celery task.
    try:
        plan = generate_search_plan(payload.intent, country=payload.country)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task = run_finder_with_plan.delay(
        intent=payload.intent,
        country=payload.country,
        results_per_query=payload.results_per_query,
        tenant_id=tenant_id,
    )

    plan_summary = {
        "country": plan.country,
        "en_queries": len(plan.search_queries_en),
        "lt_queries": len(plan.search_queries_lt),
        "industries": plan.priority_industries,
        "total_queries": len(plan.all_queries()),
        "sample_en": plan.search_queries_en[:3],
        "sample_lt": plan.search_queries_lt[:3],
    }

    return SearchPlannerRunResponse(task_id=task.id, plan_summary=plan_summary)

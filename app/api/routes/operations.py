from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.api.security import require_roles
from app.db.session import get_db
from app.models.campaign import Campaign
from app.models.company import Company, CompanyStatus
from app.models.contact import Contact
from app.models.handoff import Handoff
from app.models.message import Message, MessageDirection
from app.models.reply import Reply
from app.schemas.analytics import (
    FunnelOverview,
    KpiOverview,
    StageConversionRates,
    StageCounts,
    StageFunnelOverview,
)
from app.schemas.handoff import HandoffRead, HandoffStatusUpdate
from app.schemas.operations import OperationTaskResponse
from app.tasks.mail_operator import process_due_schedules, send_campaign_step
from app.tasks.replies import ingest_and_classify

router = APIRouter()


def _period_since(period: str) -> datetime:
    period_map = {
        "day": timedelta(days=1),
        "week": timedelta(days=7),
        "month": timedelta(days=30),
    }
    if period == "all":
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if period not in period_map:
        raise HTTPException(status_code=400, detail="invalid_period_use_day_week_month_or_all")
    return datetime.now(timezone.utc) - period_map[period]


def _pct(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


@router.post("/mail/process-due", response_model=OperationTaskResponse, status_code=202)
def trigger_process_due_mail(
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
) -> OperationTaskResponse:
    task = process_due_schedules.delay()
    return OperationTaskResponse(task_id=task.id)


@router.post("/mail/send-campaign/{campaign_id}", response_model=OperationTaskResponse, status_code=202)
def trigger_send_campaign(
    campaign_id: int,
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
) -> OperationTaskResponse:
    task = send_campaign_step.delay(campaign_id=campaign_id)
    return OperationTaskResponse(task_id=task.id)


@router.post("/replies/ingest", response_model=OperationTaskResponse, status_code=202)
def trigger_ingest_replies(
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
) -> OperationTaskResponse:
    task = ingest_and_classify.delay()
    return OperationTaskResponse(task_id=task.id)


@router.get("/handoff/warm", response_model=list[HandoffRead])
def list_warm_handoffs(
    limit: int = 50,
    status: str | None = None,
    priority: str | None = None,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("viewer", "operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> list[Handoff]:
    q = db.query(Handoff).filter(Handoff.needs_human.is_(True))
    if tenant_id is not None:
        q = q.join(Company, Company.id == Handoff.company_id).filter(Company.tenant_id == tenant_id)
    if status:
        q = q.filter(Handoff.status == status)
    if priority:
        q = q.filter(Handoff.priority == priority)
    return q.order_by(Handoff.created_at.desc()).limit(limit).all()


@router.patch("/handoff/{handoff_id}", response_model=HandoffRead)
def update_handoff_status(
    handoff_id: int,
    payload: HandoffStatusUpdate,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> Handoff:
    q = db.query(Handoff).filter(Handoff.id == handoff_id)
    if tenant_id is not None:
        q = q.join(Company, Company.id == Handoff.company_id).filter(Company.tenant_id == tenant_id)
    handoff = q.first()
    if not handoff:
        raise HTTPException(status_code=404, detail="handoff_not_found")
    handoff.status = payload.status
    db.add(handoff)
    db.commit()
    db.refresh(handoff)
    return handoff


@router.get("/analytics/kpi", response_model=KpiOverview)
def kpi_overview(
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("viewer", "operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> KpiOverview:
    companies_q = db.query(Company.id)
    if tenant_id is not None:
        companies_q = companies_q.filter(Company.tenant_id == tenant_id)

    companies_found = companies_q.count()
    companies_qualified = (
        companies_q.filter(Company.status == CompanyStatus.qualified).count()
    )
    contacts_q = db.query(Contact.id).join(Company, Company.id == Contact.company_id)
    if tenant_id is not None:
        contacts_q = contacts_q.filter(Company.tenant_id == tenant_id)
    contacts_found = contacts_q.count()

    message_q = db.query(Message.id).join(Campaign, Campaign.id == Message.campaign_id).join(Company, Company.id == Campaign.company_id)
    if tenant_id is not None:
        message_q = message_q.filter(Company.tenant_id == tenant_id)

    emails_sent = (
        message_q.filter(Message.direction == MessageDirection.outbound, Message.sent_at.isnot(None)).count()
    )
    replies_received = (
        message_q.filter(Message.direction == MessageDirection.inbound).count()
    )
    replies_q = (
        db.query(Reply.id)
        .join(Message, Message.id == Reply.message_id)
        .join(Campaign, Campaign.id == Message.campaign_id)
        .join(Company, Company.id == Campaign.company_id)
    )
    if tenant_id is not None:
        replies_q = replies_q.filter(Company.tenant_id == tenant_id)
    warm_replies = (
        replies_q.filter(Reply.label.in_(["interested", "ask_for_details", "send_rates"])).count()
    )

    return KpiOverview(
        companies_found=companies_found,
        companies_qualified=companies_qualified,
        contacts_found=contacts_found,
        emails_sent=emails_sent,
        replies_received=replies_received,
        warm_replies=warm_replies,
    )


@router.get("/analytics/funnel", response_model=FunnelOverview)
def funnel_overview(
    period: str = "week",
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("viewer", "operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> FunnelOverview:
    if period not in {"day", "week", "month"}:
        raise HTTPException(status_code=400, detail="invalid_period_use_day_week_or_month")
    since = _period_since(period)

    message_q = db.query(Message.id).join(Campaign, Campaign.id == Message.campaign_id).join(Company, Company.id == Campaign.company_id)
    if tenant_id is not None:
        message_q = message_q.filter(Company.tenant_id == tenant_id)

    emails_sent = message_q.filter(
        Message.direction == MessageDirection.outbound,
        Message.sent_at.isnot(None),
        Message.sent_at >= since,
    ).count()
    replies_received = message_q.filter(
        Message.direction == MessageDirection.inbound,
        func.coalesce(Message.received_at, Message.created_at) >= since,
    ).count()

    replies_q = (
        db.query(Reply.id)
        .join(Message, Message.id == Reply.message_id)
        .join(Campaign, Campaign.id == Message.campaign_id)
        .join(Company, Company.id == Campaign.company_id)
    )
    if tenant_id is not None:
        replies_q = replies_q.filter(Company.tenant_id == tenant_id)
    warm_replies = replies_q.filter(
        Reply.label.in_(["interested", "ask_for_details", "send_rates"]),
        Reply.created_at >= since,
    ).count()

    reply_rate = round((replies_received / emails_sent) * 100, 2) if emails_sent else 0.0
    positive_reply_rate = round((warm_replies / emails_sent) * 100, 2) if emails_sent else 0.0

    return FunnelOverview(
        period=period,
        period_start=since.isoformat(),
        emails_sent=emails_sent,
        replies_received=replies_received,
        warm_replies=warm_replies,
        reply_rate=reply_rate,
        positive_reply_rate=positive_reply_rate,
    )


@router.get("/analytics/stage-funnel", response_model=StageFunnelOverview)
def stage_funnel_overview(
    period: str = "week",
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("viewer", "operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> StageFunnelOverview:
    since = _period_since(period)

    company_q = db.query(Company.id)
    if tenant_id is not None:
        company_q = company_q.filter(Company.tenant_id == tenant_id)
    found = company_q.filter(Company.created_at >= since).count()
    qualified = company_q.filter(Company.status == CompanyStatus.qualified, Company.updated_at >= since).count()

    contacts_q = db.query(func.distinct(Contact.company_id)).join(Company, Company.id == Contact.company_id)
    if tenant_id is not None:
        contacts_q = contacts_q.filter(Company.tenant_id == tenant_id)
    contacts = (
        contacts_q.filter(Contact.created_at >= since).count()
    )

    sent_q = db.query(func.distinct(Message.campaign_id)).join(Campaign, Campaign.id == Message.campaign_id).join(Company, Company.id == Campaign.company_id)
    if tenant_id is not None:
        sent_q = sent_q.filter(Company.tenant_id == tenant_id)
    sent = (
        sent_q.filter(
            Message.direction == MessageDirection.outbound,
            Message.sent_at.isnot(None),
            Message.sent_at >= since,
        )
        .count()
    )

    replies_q = db.query(func.distinct(Message.campaign_id)).join(Campaign, Campaign.id == Message.campaign_id).join(Company, Company.id == Campaign.company_id)
    if tenant_id is not None:
        replies_q = replies_q.filter(Company.tenant_id == tenant_id)
    replies = (
        replies_q.filter(
            Message.direction == MessageDirection.inbound,
            func.coalesce(Message.received_at, Message.created_at) >= since,
        )
        .count()
    )

    warm_q = db.query(func.distinct(Handoff.campaign_id)).join(Company, Company.id == Handoff.company_id)
    if tenant_id is not None:
        warm_q = warm_q.filter(Company.tenant_id == tenant_id)
    warm = (
        warm_q.filter(Handoff.created_at >= since).count()
    )

    counts = StageCounts(
        found=found,
        qualified=qualified,
        contacts=contacts,
        sent=sent,
        replies=replies,
        warm=warm,
    )
    conversions = StageConversionRates(
        found_to_qualified=_pct(qualified, found),
        qualified_to_contacts=_pct(contacts, qualified),
        contacts_to_sent=_pct(sent, contacts),
        sent_to_replies=_pct(replies, sent),
        replies_to_warm=_pct(warm, replies),
    )

    return StageFunnelOverview(
        period=period,
        period_start=since.isoformat(),
        counts=counts,
        conversions=conversions,
    )

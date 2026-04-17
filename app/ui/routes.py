from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from redis import Redis
from sqlalchemy import desc, func, or_, text
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_id
from app.api.security import require_roles
from app.db.session import get_db
from app.models.audit_log import AuditLog
from app.models.blacklist import Blacklist
from app.models.campaign import Campaign, CampaignStatus
from app.models.company import Company, CompanyStatus
from app.models.company_page import CompanyPage
from app.models.contact import Contact
from app.models.handoff import Handoff
from app.models.message import Message, MessageDirection
from app.models.reply import Reply
from app.models.schedule import Schedule
from app.models.task import Task
from app.models.tenant_membership import MembershipRole, TenantMembership
from app.models.tenant_invite import TenantInvite
from app.models.user import User
from app.services.auth.security import hash_password, verify_password
from app.services.auth.email_tokens import create_email_token, consume_email_token
from app.services.auth.rate_limit import acquire_email_cooldown, clear_login_failures, is_login_allowed, register_login_failure
from app.services.mail.auth_emails import send_verification_email, send_password_reset_email
from app.services.auth.invites import create_invite
from app.services.auth.session_manager import resolve_active_session, revoke_session
from app.api.routes.auth import accept_invite as api_accept_invite
from app.schemas.auth import AcceptInviteRequest
from app.services.contact_resolver.resolver import resolve_contacts_for_company, save_contacts
from app.services.finder.search_planner import generate_search_plan
from app.services.mail.zone_mail import check_zone_connectivity
from app.services.mail.invites import send_invite_email
from app.services.outreach.policy import get_blacklist_skip_reason, is_country_allowed_for_outreach, language_for_country
from app.services.outreach.writer import generate_outreach_sequence
from app.services.qualifier.openai_qualifier import qualify_company
from app.services.researcher.site_researcher import PageSnapshot, ResearchResult, research_company_site
from app.tasks.finder import run_finder, run_finder_with_plan
from app.tasks.mail_operator import process_due_schedules, send_campaign_step
from app.tasks.outreach import generate_outreach_for_company
from app.tasks.replies import ingest_and_classify
from app.tasks.researcher import run_research_and_qualify
from app.worker.celery_app import celery_app
from app.core.config import settings

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

router = APIRouter(prefix="/ui", tags=["ui"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _to_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    return None


def _to_dt(value: str | None, at_end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None

    if at_end_of_day:
        return datetime.combine(parsed_date, time.max)
    return datetime.combine(parsed_date, time.min)


def _trim(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _json_excerpt(payload: dict | None, limit: int = 160) -> str:
    if not payload:
        return "-"
    text = str(payload)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _event_level(action: str) -> str:
    action_low = (action or "").lower()
    if any(token in action_low for token in ["error", "failed", "unmatched"]):
        return "error"
    if any(token in action_low for token in ["skipped", "stopped", "deferred"]):
        return "warning"
    if any(
        token in action_low
        for token in ["sent", "generated", "qualified", "updated", "requested", "ingest", "handoff"]
    ):
        return "success"
    return "info"


def _nav_key(path: str) -> str:
    if path == "/ui" or path == "/ui/":
        return "dashboard"
    if path.startswith("/ui/companies"):
        return "companies"
    if path.startswith("/ui/campaigns"):
        return "campaigns"
    if path.startswith("/ui/messages"):
        return "messages"
    if path.startswith("/ui/replies"):
        return "replies"
    if path.startswith("/ui/operations"):
        return "operations"
    if path.startswith("/ui/contacts"):
        return "contacts"
    if path.startswith("/ui/handoffs"):
        return "handoffs"
    if path.startswith("/ui/actions"):
        return "actions"
    if path.startswith("/ui/team"):
        return "team"
    return ""


def _service_status(ok: bool, detail: str | None = None) -> dict[str, str | bool | None]:
    return {
        "ok": ok,
        "label": "online" if ok else "offline",
        "detail": detail,
    }


def _normalize_status(value: str | object | None) -> str:
    if value is None:
        return ""
    raw = str(value).strip().lower()
    if "." in raw:
        raw = raw.split(".")[-1]
    return raw


def _status_label(value: str | object | None) -> str:
    normalized = _normalize_status(value)
    labels = {
        "new": "Новая",
        "researching": "Исследование",
        "qualifying": "Квалификация",
        "qualified": "Квалифицирована",
        "rejected": "Отклонена",
        "outreaching": "В outreach",
        "replied": "Ответ получен",
        "closed": "Закрыта",
    }
    return labels.get(normalized, normalized or "-")


def _format_compact_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    dt = value
    now = datetime.utcnow()
    if dt.date() == now.date():
        return f"сегодня, {dt.strftime('%H:%M')}"
    month_map = {
        1: "янв",
        2: "фев",
        3: "мар",
        4: "апр",
        5: "май",
        6: "июн",
        7: "июл",
        8: "авг",
        9: "сен",
        10: "окт",
        11: "ноя",
        12: "дек",
    }
    return f"{dt.day:02d} {month_map.get(dt.month, dt.month)} {dt.strftime('%H:%M')}"


def _get_system_health(db: Session, smtp_ok: bool, imap_ok: bool, mail_error: str | None) -> dict[str, dict]:
    db_ok = True
    db_error = None
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_error = str(exc)

    redis_ok = False
    redis_error = None
    try:
        redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        redis_ok = bool(redis_client.ping())
    except Exception as exc:
        redis_error = str(exc)

    worker_ok = False
    worker_error = None
    try:
        ping_result = celery_app.control.ping(timeout=1)
        worker_ok = bool(ping_result)
        if not worker_ok:
            worker_error = "нет ответа ping"
    except Exception as exc:
        worker_error = str(exc)

    beat_ok = False
    beat_error = None
    beat_file = Path(__file__).resolve().parents[2] / "celerybeat-schedule"
    if beat_file.exists():
        modified = datetime.utcfromtimestamp(beat_file.stat().st_mtime)
        beat_ok = (datetime.utcnow() - modified) <= timedelta(minutes=15)
        if not beat_ok:
            beat_error = "нет обновления файла расписания > 15 мин"
    else:
        beat_error = "файл celerybeat-schedule не найден"

    return {
        "api": _service_status(True),
        "smtp": _service_status(smtp_ok, mail_error if not smtp_ok else None),
        "imap": _service_status(imap_ok, mail_error if not imap_ok else None),
        "worker": _service_status(worker_ok, worker_error),
        "beat": _service_status(beat_ok, beat_error),
        "db": _service_status(db_ok, db_error),
        "redis": _service_status(redis_ok, redis_error),
    }


def _research_result_from_pages(domain: str, pages: list[CompanyPage]) -> ResearchResult:
    snapshots = [
        PageSnapshot(
            url=page.url,
            page_type=page.page_type or "page",
            text=page.raw_text or "",
        )
        for page in pages
    ]
    text_summary = "\n".join((page.raw_text or "")[:2000] for page in pages)[:5000]
    low = text_summary.lower()
    langs: list[str] = []
    for marker in ["english", "deutsch", "lietuvi", "latvie", "eesti", "polski", "francais"]:
        if marker in low:
            langs.append(marker)

    has_careers = any((page.page_type or "") in {"careers", "jobs"} for page in pages)
    return ResearchResult(
        domain=domain,
        pages=snapshots,
        languages_found=langs,
        has_careers_page=has_careers,
        text_summary=text_summary,
    )


def _pipeline_state(company: Company, pages: list[CompanyPage], contacts: list[Contact], outreach_draft: AuditLog | None) -> dict[str, str]:
    return {
        "research": "done" if pages else "pending",
        "contacts": "done" if contacts else "pending",
        "qualification": "done" if company.qualification_result else "pending",
        "outreach": "done" if outreach_draft else "pending",
    }


def _detect_text_language(text_value: str | None) -> str:
    text = (text_value or "").lower()
    if not text:
        return "-"

    has_ru = bool(re.search(r"[а-яё]", text))
    lt_markers = [
        "gamyba",
        "metalo",
        "suvir",
        "apdirb",
        "bald",
        "medien",
        "pramon",
        "statyb",
        "maisto",
        "sand",
    ]
    en_markers = [
        "manufacturing",
        "metal",
        "welding",
        "machining",
        "furniture",
        "wood",
        "industrial",
        "construction",
        "food",
        "logistics",
    ]

    has_lt = any(marker in text for marker in lt_markers)
    has_en = any(marker in text for marker in en_markers)

    langs: list[str] = []
    if has_lt:
        langs.append("LT")
    if has_en:
        langs.append("EN")
    if has_ru:
        langs.append("RU")

    if not langs:
        if any(ord(ch) > 127 for ch in text):
            return "LT"
        return "EN"
    return "/".join(langs)


def _classify_industry(industry: str | None) -> dict[str, str]:
    source = (industry or "").strip()
    text = source.lower()

    category = "Прочее / неясно"
    subcategory = "не определено"
    category_key = "other"
    bucket = "other"

    def contains(*parts: str) -> bool:
        return any(part in text for part in parts)

    if contains("metal construction", "metalo konstruk", "steel structure", "plieno konstruk"):
        category = "Металлоконструкции"
        category_key = "metal_construction"
        bucket = "metal"
        if contains("suvir", "weld"):
            subcategory = "сварка и сборка"
        elif contains("tube", "pipe", "vamzd"):
            subcategory = "трубопроводы"
        else:
            subcategory = "металлокаркас"
    elif contains("cnc", "machining", "frez", "turning", "tekin", "stakl"):
        category = "Мехобработка / CNC"
        category_key = "cnc"
        bucket = "metal"
        if contains("turn", "tekin"):
            subcategory = "токарка"
        elif contains("milling", "frez"):
            subcategory = "фрезеровка"
        else:
            subcategory = "токарка / фрезеровка"
    elif contains("metal fabrication", "sheet metal", "laser", "metalo apdirb", "lakst", "lankst"):
        category = "Металлообработка"
        category_key = "metal_processing"
        bucket = "metal"
        if contains("sheet", "lakst"):
            subcategory = "листовой металл"
        elif contains("paint", "powder", "coating"):
            subcategory = "покраска"
        elif contains("stainless", "nerz", "inox"):
            subcategory = "нержавейка"
        else:
            subcategory = "резка и обработка"
    elif contains("furniture", "bald", "sofa", "kitchen"):
        category = "Мебельное производство"
        category_key = "furniture"
        bucket = "furniture"
        if contains("soft", "sofa", "minkst"):
            subcategory = "мягкая мебель"
        else:
            subcategory = "корпусная мебель"
    elif contains("wood", "timber", "medien", "stolar"):
        category = "Деревообработка"
        category_key = "wood"
        bucket = "wood"
        subcategory = "пиломатериалы и обработка"
    elif contains("machine", "machinery", "engineer", "mechanical", "ireng"):
        category = "Машиностроение"
        category_key = "machinery"
        bucket = "metal"
        subcategory = "узлы и агрегаты"
    elif contains("equipment", "industrial equipment", "conveyor", "automation line"):
        category = "Промышленное оборудование"
        category_key = "industrial_equipment"
        bucket = "metal"
        subcategory = "промышленное оборудование"
    elif contains("electrical", "electric", "automation", "elektr", "automatik"):
        category = "Электрика / автоматика"
        category_key = "electro"
        bucket = "electric"
        subcategory = "электромонтаж и автоматика"
    elif contains("construction", "statyb", "montage", "installation"):
        category = "Строительство / монтаж"
        category_key = "construction"
        bucket = "construction"
        subcategory = "монтаж и сервис"
    elif contains("logistics", "warehouse", "sandel", "transport"):
        category = "Логистика / склад"
        category_key = "logistics"
        bucket = "other"
        subcategory = "складская логистика"
    elif contains("food", "maisto", "beverage", "bakery"):
        category = "Пищевая промышленность"
        category_key = "food"
        bucket = "food"
        subcategory = "пищевое производство"

    language_chip = _detect_text_language(source)
    return {
        "category": category,
        "subcategory": subcategory,
        "source": source or "-",
        "category_key": category_key,
        "bucket": bucket,
        "language_chip": language_chip,
    }


def _operator_relevance(company: Company) -> dict[str, str]:
    status = _normalize_status(company.status)
    qualification = company.qualification_result or {}
    is_relevant = bool(qualification.get("is_relevant")) if isinstance(qualification, dict) else False
    score_raw = company.score
    score_percent = 0.0
    if score_raw is not None:
        score_percent = score_raw * 100.0 if score_raw <= 1.0 else score_raw

    if status in {"qualified", "outreaching", "replied"} or is_relevant or score_percent >= 75:
        return {"label": "Подходит", "level": "fit"}
    if status in {"new", "researching", "qualifying"} or score_percent >= 45:
        return {"label": "Возможно", "level": "maybe"}
    return {"label": "Не наш профиль", "level": "no_fit"}


def _flash_redirect(url: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    params: dict[str, str] = {}
    if message:
        params["msg"] = message
    if error:
        params["err"] = error
    if params:
        glue = "&" if "?" in url else "?"
        url = f"{url}{glue}{urlencode(params)}"
    return RedirectResponse(url=url, status_code=303)


def _base_context(request: Request, page_title: str) -> dict:
    return {
        "request": request,
        "page_title": page_title,
        "flash_message": request.query_params.get("msg"),
        "flash_error": request.query_params.get("err"),
        "now": datetime.utcnow(),
        "active_nav": _nav_key(request.url.path),
        "csrf_cookie_name": settings.auth_csrf_cookie_name,
    }


def _audit_auth_event(
    db: Session,
    *,
    action: str,
    user_id: int | None = None,
    reason: str | None = None,
    details: dict | None = None,
) -> None:
    try:
        db.add(
            AuditLog(
                entity_type="auth",
                entity_id=user_id,
                action=action,
                details=details,
                reason=reason,
            )
        )
        db.commit()
    except Exception:
        db.rollback()


@router.post("/logout")
def ui_logout(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    token = request.cookies.get(settings.auth_session_cookie_name)
    if token:
        session = resolve_active_session(db, token)
        if session is not None:
            revoke_session(db, session)
            _audit_auth_event(
                db,
                action="ui_auth_logout",
                user_id=session.user_id,
                details={"tenant_id": session.tenant_id},
            )

    response = _flash_redirect("/ui", message="Вы вышли из системы")
    response.delete_cookie(settings.auth_session_cookie_name, path="/")
    response.delete_cookie(settings.auth_csrf_cookie_name, path="/")
    return response


def _company_query(db: Session, tenant_id: int | None):
    q = db.query(Company)
    if tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    return q


def _company_by_id(db: Session, company_id: int, tenant_id: int | None) -> Company | None:
    return _company_query(db, tenant_id).filter(Company.id == company_id).first()


def _campaign_query(db: Session, tenant_id: int | None):
    q = db.query(Campaign)
    if tenant_id is not None:
        q = q.join(Company, Company.id == Campaign.company_id).filter(Company.tenant_id == tenant_id)
    return q


def _campaign_by_id(db: Session, campaign_id: int, tenant_id: int | None) -> Campaign | None:
    return _campaign_query(db, tenant_id).filter(Campaign.id == campaign_id).first()


def _handoff_query(db: Session, tenant_id: int | None):
    q = db.query(Handoff)
    if tenant_id is not None:
        q = q.join(Company, Company.id == Handoff.company_id).filter(Company.tenant_id == tenant_id)
    return q


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    companies_q = _company_query(db, tenant_id)
    status_counts_rows = companies_q.with_entities(Company.status, func.count(Company.id)).group_by(Company.status).all()
    status_counts = {str(status): count for status, count in status_counts_rows}

    company_total = companies_q.count()
    active_campaigns = (
        _campaign_query(db, tenant_id)
        .filter(Campaign.status == CampaignStatus.active)
        .count()
    )
    message_q = db.query(Message).join(Campaign, Campaign.id == Message.campaign_id).join(Company, Company.id == Campaign.company_id)
    if tenant_id is not None:
        message_q = message_q.filter(Company.tenant_id == tenant_id)
    sent_messages = (
        message_q.filter(Message.direction == MessageDirection.outbound, Message.sent_at.isnot(None)).count()
    )
    replies_count = (
        message_q.filter(Message.direction == MessageDirection.inbound).count()
    )
    handoff_q = _handoff_query(db, tenant_id)
    warm_handoffs = (
        handoff_q.filter(Handoff.needs_human.is_(True)).count()
    )

    recent_activity = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(25).all()

    smtp_ok = False
    imap_ok = False
    mail_error = None
    try:
        connectivity = check_zone_connectivity()
        smtp_ok = bool(connectivity.smtp_ok)
        imap_ok = bool(connectivity.imap_ok)
    except Exception as exc:
        mail_error = str(exc)

    system_health = _get_system_health(db=db, smtp_ok=smtp_ok, imap_ok=imap_ok, mail_error=mail_error)

    rejected_recent = (
        companies_q
        .filter(Company.status == CompanyStatus.rejected, Company.updated_at >= datetime.utcnow() - timedelta(days=2))
        .count()
    )
    handoffs_new = (
        handoff_q.filter(Handoff.status == "new", Handoff.needs_human.is_(True)).count()
    )
    unsent_outbound = (
        message_q
        .filter(
            Message.direction == MessageDirection.outbound,
            Message.sent_at.is_(None),
            Campaign.status == CampaignStatus.active,
            Campaign.has_reply.is_(False),
        )
        .count()
    )

    attention_items: list[dict[str, str | int]] = []
    if handoffs_new > 0:
        attention_items.append({"title": "Новые warm handoff", "value": handoffs_new, "level": "warning"})
    if unsent_outbound > 0:
        attention_items.append({"title": "Ожидают отправки", "value": unsent_outbound, "level": "info"})
    if rejected_recent > 0:
        attention_items.append({"title": "Отклонено за 48ч", "value": rejected_recent, "level": "warning"})
    down_services = [name.upper() for name, status in system_health.items() if not status.get("ok")]
    if down_services:
        attention_items.append({
            "title": "Сервисы offline",
            "value": ", ".join(down_services),
            "level": "error",
        })

    last_planner_summary = (
        db.query(AuditLog)
        .filter(AuditLog.action.in_(["planner_preview", "plan_and_run_requested"]))
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    last_finder_summary = (
        db.query(AuditLog)
        .filter(AuditLog.action.in_(["finder_run_requested", "plan_and_run_requested"]))
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    last_reply_ingest_summary = (
        db.query(AuditLog)
        .filter(AuditLog.action == "reply_ingest_requested")
        .order_by(AuditLog.created_at.desc())
        .first()
    )

    summaries = [
        {"title": "Планировщик поиска", "row": last_planner_summary},
        {"title": "Поиск компаний", "row": last_finder_summary},
        {"title": "Обработка ответов", "row": last_reply_ingest_summary},
    ]
    summaries_non_empty = [item for item in summaries if item["row"] is not None]

    recent_tasks = db.query(Task).order_by(Task.created_at.desc()).limit(8).all()

    activity_rows = [
        {
            "row": row,
            "level": _event_level(row.action),
            "details_full": str(row.details) if row.details else "-",
        }
        for row in recent_activity
    ]

    context = {
        **_base_context(request, "Панель"),
        "company_total": company_total,
        "status_counts": status_counts,
        "active_campaigns": active_campaigns,
        "sent_messages": sent_messages,
        "replies_count": replies_count,
        "warm_handoffs": warm_handoffs,
        "recent_activity": activity_rows,
        "health": {
            "api_ok": True,
            "smtp_ok": smtp_ok,
            "imap_ok": imap_ok,
            "mail_error": mail_error,
        },
        "system_health": system_health,
        "attention_items": attention_items,
        "status_compact": {
            "new": status_counts.get("new", 0),
            "qualified": status_counts.get("qualified", 0),
            "rejected": status_counts.get("rejected", 0),
        },
        "last_planner_summary": last_planner_summary,
        "last_finder_summary": last_finder_summary,
        "last_reply_ingest_summary": last_reply_ingest_summary,
        "summaries": summaries_non_empty,
        "recent_tasks": recent_tasks,
        "json_excerpt": _json_excerpt,
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/companies", response_class=HTMLResponse)
def companies_page(
    request: Request,
    country: str | None = None,
    status: str | None = None,
    industry: str | None = None,
    industry_bucket: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    has_contacts: str | None = None,
    has_campaign: str | None = None,
    q: str | None = None,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    has_contacts_bool = _to_bool(has_contacts)
    has_campaign_bool = _to_bool(has_campaign)

    contacts_exists = db.query(Contact.id).filter(Contact.company_id == Company.id).exists()
    campaign_exists = db.query(Campaign.id).filter(Campaign.company_id == Company.id).exists()
    draft_exists = (
        db.query(AuditLog.id)
        .filter(
            AuditLog.entity_type == "company",
            AuditLog.entity_id == Company.id,
            AuditLog.action == "ui_outreach_draft_generated",
        )
        .exists()
    )

    query = db.query(Company, contacts_exists.label("has_contacts"), campaign_exists.label("has_campaign"), draft_exists.label("has_draft"))
    if tenant_id is not None:
        query = query.filter(Company.tenant_id == tenant_id)

    if country:
        query = query.filter(Company.country == country)
    if status:
        query = query.filter(Company.status == status)
    if industry:
        query = query.filter(Company.industry.ilike(f"%{industry}%"))
    if score_min is not None:
        query = query.filter(Company.score.isnot(None), Company.score >= score_min)
    if score_max is not None:
        query = query.filter(Company.score.isnot(None), Company.score <= score_max)
    if has_contacts_bool is True:
        query = query.filter(contacts_exists)
    elif has_contacts_bool is False:
        query = query.filter(~contacts_exists)
    if has_campaign_bool is True:
        query = query.filter(campaign_exists)
    elif has_campaign_bool is False:
        query = query.filter(~campaign_exists)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Company.domain.ilike(like), Company.name.ilike(like)))

    base_rows = query.order_by(Company.created_at.desc()).limit(300).all()

    rows = []
    for company, has_contacts, has_campaign, has_draft in base_rows:
        profile = _classify_industry(company.industry)
        relevance = _operator_relevance(company)
        row = {
            "company": company,
            "has_contacts": bool(has_contacts),
            "has_campaign": bool(has_campaign),
            "has_draft": bool(has_draft),
            "profile": profile,
            "relevance": relevance,
        }
        rows.append(row)

    normalized_bucket = (industry_bucket or "").strip().lower()
    if normalized_bucket and normalized_bucket not in {"", "all"}:
        rows = [row for row in rows if row["profile"]["bucket"] == normalized_bucket]

    summary = {
        "total": len(rows),
        "qualified": sum(1 for row in rows if _normalize_status(row["company"].status) == "qualified"),
        "with_contacts": sum(1 for row in rows if row["has_contacts"]),
        "without_campaign": sum(1 for row in rows if not row["has_campaign"]),
        "unprepared": sum(
            1 for row in rows
            if row["company"].qualification_result is None or not row["has_contacts"]
        ),
    }

    meta_companies_q = _company_query(db, tenant_id)
    countries = [row[0] for row in meta_companies_q.with_entities(Company.country).filter(Company.country.isnot(None)).distinct().order_by(Company.country.asc()).all()]
    industries = [row[0] for row in meta_companies_q.with_entities(Company.industry).filter(Company.industry.isnot(None)).distinct().order_by(Company.industry.asc()).all()]

    context = {
        **_base_context(request, "Компании"),
        "rows": rows,
        "countries": countries,
        "industries": industries,
        "statuses": [status.value for status in CompanyStatus],
        "summary": summary,
        "status_label": _status_label,
        "normalize_status": _normalize_status,
        "format_compact_dt": _format_compact_dt,
        "industry_bucket": normalized_bucket,
        "bucket_options": [
            {"key": "all", "label": "Все"},
            {"key": "metal", "label": "Металл"},
            {"key": "furniture", "label": "Мебель"},
            {"key": "wood", "label": "Дерево"},
            {"key": "food", "label": "Пищевая"},
            {"key": "electric", "label": "Электрика"},
            {"key": "construction", "label": "Стройка"},
            {"key": "other", "label": "Прочее"},
        ],
        "filters": {
            "country": country or "",
            "status": status or "",
            "industry_bucket": normalized_bucket,
            "industry": industry or "",
            "score_min": "" if score_min is None else score_min,
            "score_max": "" if score_max is None else score_max,
            "has_contacts": has_contacts or "",
            "has_campaign": has_campaign or "",
            "q": q or "",
        },
    }
    return templates.TemplateResponse(request, "companies.html", context)


@router.post("/companies/prepare-all")
def companies_prepare_all(
    country: str | None = Form(None),
    status: str | None = Form(None),
    industry: str | None = Form(None),
    industry_bucket: str | None = Form(None),
    score_min: str | None = Form(None),
    score_max: str | None = Form(None),
    has_contacts: str | None = Form(None),
    has_campaign: str | None = Form(None),
    q: str | None = Form(None),
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
):
    from fastapi.responses import JSONResponse
    from app.tasks.pipeline import launch_prepare_all

    score_min_f = float(score_min) if score_min and score_min.strip() else None
    score_max_f = float(score_max) if score_max and score_max.strip() else None

    # Companies needing work: no qualification yet OR qualified but no contacts found yet
    contacts_exists_sub = db.query(Contact.id).filter(Contact.company_id == Company.id).exists()
    needs_work = (Company.qualification_result.is_(None)) | (~contacts_exists_sub)

    contacts_exists = db.query(Contact.id).filter(Contact.company_id == Company.id).exists()
    campaign_exists = db.query(Campaign.id).filter(Campaign.company_id == Company.id).exists()
    query = db.query(Company.id).filter(needs_work)
    if tenant_id is not None:
        query = query.filter(Company.tenant_id == tenant_id)

    if country:
        query = query.filter(Company.country == country)
    if status:
        query = query.filter(Company.status == status)
    if industry:
        query = query.filter(Company.industry.ilike(f"%{industry}%"))
    if score_min_f is not None:
        query = query.filter(Company.score.isnot(None), Company.score >= score_min_f)
    if score_max_f is not None:
        query = query.filter(Company.score.isnot(None), Company.score <= score_max_f)
    if has_contacts == "true":
        query = query.filter(contacts_exists)
    elif has_contacts == "false":
        query = query.filter(~contacts_exists)
    if has_campaign == "true":
        query = query.filter(campaign_exists)
    elif has_campaign == "false":
        query = query.filter(~campaign_exists)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Company.domain.ilike(like), Company.name.ilike(like)))
    if industry_bucket and industry_bucket not in {"", "all"}:
        # post-filter not possible in SQL since bucket is computed, query without it and filter in Python
        pass

    company_ids = [row[0] for row in query.limit(500).all()]
    if not company_ids:
        return JSONResponse({"error": "no_companies"}, status_code=400)

    job_id = launch_prepare_all(company_ids, tenant_id=tenant_id)
    return JSONResponse({"job_id": job_id, "total": len(company_ids)})


@router.get("/companies/prepare-all/status")
def companies_prepare_all_status(job_id: str):
    from fastapi.responses import JSONResponse
    from app.tasks.pipeline import get_job_progress

    progress = get_job_progress(job_id)
    if not progress:
        return JSONResponse({"error": "job_not_found"}, status_code=404)
    return JSONResponse(progress)


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def company_detail(
    request: Request,
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    company = _company_by_id(db, company_id, tenant_id)
    if not company:
        raise HTTPException(status_code=404, detail="company_not_found")

    pages = db.query(CompanyPage).filter(CompanyPage.company_id == company_id).order_by(CompanyPage.collected_at.desc()).all()
    contacts = db.query(Contact).filter(Contact.company_id == company_id).order_by(Contact.confidence.desc().nullslast(), Contact.id.asc()).all()
    campaigns = db.query(Campaign).filter(Campaign.company_id == company_id).order_by(Campaign.created_at.desc()).all()

    campaign_ids = [campaign.id for campaign in campaigns]
    messages_by_campaign: dict[int, list[Message]] = {}
    if campaign_ids:
        messages = (
            db.query(Message)
            .filter(Message.campaign_id.in_(campaign_ids))
            .order_by(Message.campaign_id.asc(), Message.created_at.desc())
            .all()
        )
        for message in messages:
            messages_by_campaign.setdefault(message.campaign_id, []).append(message)

    skip_logs = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "company",
            AuditLog.entity_id == company_id,
            AuditLog.action.in_(["outreach_skipped", "qualified_but_skipped_country"]),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(20)
        .all()
    )

    domain_blacklist = (
        db.query(Blacklist)
        .filter(Blacklist.entry_type == "domain", Blacklist.value == company.domain)
        .first()
    )
    emails = [c.email for c in contacts if c.email]
    blacklisted_contacts = []
    if emails:
        blacklisted_contacts = (
            db.query(Blacklist)
            .filter(Blacklist.entry_type == "email", Blacklist.value.in_(emails))
            .all()
        )

    qualification = company.qualification_result or {}
    outreach_draft = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "company",
            AuditLog.entity_id == company_id,
            AuditLog.action == "ui_outreach_draft_generated",
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    pipeline_status = _pipeline_state(company=company, pages=pages, contacts=contacts, outreach_draft=outreach_draft)

    context = {
        **_base_context(request, f"Компания #{company.id}"),
        "company": company,
        "qualification": qualification,
        "signals": qualification.get("signals", []) if isinstance(qualification, dict) else [],
        "pages": pages,
        "contacts": contacts,
        "campaigns": campaigns,
        "messages_by_campaign": messages_by_campaign,
        "skip_logs": skip_logs,
        "domain_blacklist": domain_blacklist,
        "blacklisted_contacts": blacklisted_contacts,
        "outreach_draft": outreach_draft,
        "pipeline_status": pipeline_status,
        "json_excerpt": _json_excerpt,
    }
    return templates.TemplateResponse(request, "company_detail.html", context)


@router.post("/companies/{company_id}/prepare")
def company_action_prepare(
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    company = _company_by_id(db, company_id, tenant_id)
    if not company:
        return _flash_redirect("/ui/companies", error="Компания не найдена")

    pages = db.query(CompanyPage).filter(CompanyPage.company_id == company_id).all()
    contacts = db.query(Contact).filter(Contact.company_id == company_id).all()
    existing_draft = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "company",
            AuditLog.entity_id == company_id,
            AuditLog.action == "ui_outreach_draft_generated",
        )
        .first()
    )

    step_notes: list[str] = []
    research_result: ResearchResult | None = None

    if not pages:
        research_result = research_company_site(company.domain)
        existing_urls = {url for (url,) in db.query(CompanyPage.url).filter(CompanyPage.company_id == company_id).all()}
        for p in research_result.pages:
            if p.url in existing_urls:
                continue
            db.add(
                CompanyPage(
                    company_id=company.id,
                    url=p.url,
                    page_type=p.page_type,
                    raw_text=p.text,
                )
            )
            existing_urls.add(p.url)
        db.add(
            AuditLog(
                entity_type="company",
                entity_id=company.id,
                action="site_researched",
                details={
                    "pages_found": len(research_result.pages),
                    "languages_found": research_result.languages_found,
                    "has_careers_page": research_result.has_careers_page,
                    "source": "ui_prepare_pipeline",
                },
                reason="Research выполнен из кнопки 'Подготовить компанию'.",
            )
        )
        step_notes.append("Исследование: выполнено")
        pages = db.query(CompanyPage).filter(CompanyPage.company_id == company_id).all()
    else:
        step_notes.append("Исследование: уже было")

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
        if qualification.get("is_relevant") and country_allowed:
            company.status = CompanyStatus.qualified
        elif qualification.get("is_relevant") and not country_allowed:
            company.status = CompanyStatus.rejected
        else:
            company.status = CompanyStatus.rejected

        db.add(
            AuditLog(
                entity_type="company",
                entity_id=company.id,
                action="company_qualified",
                details={**qualification, "source": "ui_prepare_pipeline"},
                reason="Qualification выполнен из кнопки 'Подготовить компанию'.",
            )
        )
        step_notes.append("Qualification: выполнено")
    else:
        step_notes.append("Qualification: уже было")

    if not contacts:
        resolved_contacts = resolve_contacts_for_company(company.id, db)
        created = save_contacts(company.id, resolved_contacts, db)
        contacts = db.query(Contact).filter(Contact.company_id == company_id).all()
        db.add(
            AuditLog(
                entity_type="company",
                entity_id=company.id,
                action="contacts_resolved",
                details={"created": len(created), "source": "ui_prepare_pipeline"},
                reason="Контакты найдены из кнопки 'Подготовить компанию'.",
            )
        )
        step_notes.append(f"Контакты: выполнено (+{len(created)})")
    else:
        step_notes.append("Контакты: уже были")

    if not existing_draft:
        qualification_data = company.qualification_result or {}
        top_contact = (
            db.query(Contact)
            .filter(Contact.company_id == company_id)
            .order_by(Contact.confidence.desc().nullslast(), Contact.id.asc())
            .first()
        )
        if top_contact and is_country_allowed_for_outreach(company.country):
            skip_reason = get_blacklist_skip_reason(
                company_domain=company.domain,
                contact_email=top_contact.email,
                db=db,
            )
            if skip_reason:
                db.add(
                    AuditLog(
                        entity_type="company",
                        entity_id=company.id,
                        action="ui_outreach_draft_skipped",
                        details={"reason": skip_reason},
                        reason="Draft outreach пропущен из-за blacklist.",
                    )
                )
                step_notes.append(f"Outreach: пропущено ({skip_reason})")
            else:
                brief = (
                    f"Company {company.name or company.domain} ({company.domain}), "
                    f"industry={company.industry or 'unknown'}, score={company.score}, "
                    f"signals={qualification_data.get('signals', [])}"
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
                db.add(
                    AuditLog(
                        entity_type="company",
                        entity_id=company.id,
                        action="ui_outreach_draft_generated",
                        details={
                            "contact_id": top_contact.id,
                            "contact_email": top_contact.email,
                            "language": language,
                            "subject": sequence.get("subject"),
                            "body_step_1": sequence.get("body_step_1"),
                            "body_followup_1": sequence.get("body_followup_1"),
                            "body_followup_2": sequence.get("body_followup_2"),
                        },
                        reason="Outreach draft сформирован без создания кампании.",
                    )
                )
                step_notes.append("Outreach: draft выполнен")
        else:
            db.add(
                AuditLog(
                    entity_type="company",
                    entity_id=company.id,
                    action="ui_outreach_draft_skipped",
                    details={
                        "reason": "no_contact_or_country_not_allowed",
                        "country": company.country,
                    },
                    reason="Draft outreach пропущен: нет контакта или страна не разрешена.",
                )
            )
            step_notes.append("Outreach: pending (нет контакта/страна)")
    else:
        step_notes.append("Outreach: уже был draft")

    db.add(company)
    db.commit()
    return _flash_redirect(
        f"/ui/companies/{company_id}",
        message=" | ".join(step_notes),
    )


@router.post("/companies/{company_id}/send-draft")
def company_action_send_draft(
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Create campaign from saved draft and send it immediately."""
    company = _company_by_id(db, company_id, tenant_id)
    if not company:
        return _flash_redirect("/ui/companies", error="Компания не найдена")

    draft = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "company",
            AuditLog.entity_id == company_id,
            AuditLog.action == "ui_outreach_draft_generated",
        )
        .first()
    )
    if not draft:
        return _flash_redirect(
            "/ui/companies",
            error=f"Нет подготовленного письма для {company.domain}. Сначала нажмите «Подготовить».",
        )

    existing_campaign = (
        _campaign_query(db, tenant_id).filter(Campaign.company_id == company_id, Campaign.status == CampaignStatus.active).first()
    )
    if existing_campaign:
        return _flash_redirect(
            "/ui/companies",
            error=f"Уже есть активная кампания #{existing_campaign.id} для {company.domain}.",
        )

    details = draft.details or {}
    contact_id = details.get("contact_id")
    language = details.get("language", "lt")

    campaign = Campaign(
        company_id=company_id,
        contact_id=contact_id,
        status=CampaignStatus.active,
        language=language,
        step=0,
    )
    db.add(campaign)
    db.flush()

    db.add(
        AuditLog(
            entity_type="campaign",
            entity_id=campaign.id,
            action="campaign_created_from_draft",
            details={"source": "ui_send_draft", "draft_audit_id": draft.id},
            reason="Кампания создана из draft через кнопку «Отправить» на странице компаний.",
        )
    )
    db.commit()

    task = send_campaign_step.delay(campaign_id=campaign.id)
    return _flash_redirect(
        "/ui/companies",
        message=f"Отправка запущена для {company.domain} (кампания #{campaign.id}, задача {task.id})",
    )


@router.get("/contacts", response_class=HTMLResponse)
def contacts_page(
    request: Request,
    role: str | None = None,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    q: str | None = None,
    country: str | None = None,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = db.query(Contact, Company).join(Company, Company.id == Contact.company_id)
    if tenant_id is not None:
        query = query.filter(Company.tenant_id == tenant_id)

    if role:
        query = query.filter(Contact.role.ilike(f"%{role}%"))
    if confidence_min is not None:
        query = query.filter(Contact.confidence.isnot(None), Contact.confidence >= confidence_min)
    if confidence_max is not None:
        query = query.filter(Contact.confidence.isnot(None), Contact.confidence <= confidence_max)
    if country:
        query = query.filter(Company.country == country)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Company.domain.ilike(like),
                Company.name.ilike(like),
                Contact.email.ilike(like),
            )
        )

    rows = query.order_by(Contact.created_at.desc()).limit(300).all()

    roles_q = db.query(Contact.role).join(Company, Company.id == Contact.company_id)
    if tenant_id is not None:
        roles_q = roles_q.filter(Company.tenant_id == tenant_id)
    roles = [row[0] for row in roles_q.filter(Contact.role.isnot(None)).distinct().order_by(Contact.role.asc()).all()]
    countries_q = db.query(Company.country)
    if tenant_id is not None:
        countries_q = countries_q.filter(Company.tenant_id == tenant_id)
    countries = [row[0] for row in countries_q.filter(Company.country.isnot(None)).distinct().order_by(Company.country.asc()).all()]

    context = {
        **_base_context(request, "Контакты"),
        "rows": rows,
        "roles": roles,
        "countries": countries,
        "filters": {
            "role": role or "",
            "confidence_min": "" if confidence_min is None else confidence_min,
            "confidence_max": "" if confidence_max is None else confidence_max,
            "q": q or "",
            "country": country or "",
        },
    }
    return templates.TemplateResponse(request, "contacts.html", context)


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(
    request: Request,
    status: str | None = None,
    language: str | None = None,
    has_reply: str | None = None,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = (
        db.query(Campaign, Company, Contact)
        .join(Company, Company.id == Campaign.company_id)
        .outerjoin(Contact, Contact.id == Campaign.contact_id)
    )
    if tenant_id is not None:
        query = query.filter(Company.tenant_id == tenant_id)

    if status:
        query = query.filter(Campaign.status == status)
    if language:
        query = query.filter(Campaign.language == language)

    has_reply_bool = _to_bool(has_reply)
    if has_reply_bool is True:
        query = query.filter(Campaign.has_reply.is_(True))
    elif has_reply_bool is False:
        query = query.filter(Campaign.has_reply.is_(False))

    rows = query.order_by(Campaign.updated_at.desc()).limit(300).all()
    languages_q = db.query(Campaign.language).join(Company, Company.id == Campaign.company_id)
    if tenant_id is not None:
        languages_q = languages_q.filter(Company.tenant_id == tenant_id)
    languages = [row[0] for row in languages_q.filter(Campaign.language.isnot(None)).distinct().order_by(Campaign.language.asc()).all()]

    context = {
        **_base_context(request, "Кампании"),
        "rows": rows,
        "languages": languages,
        "statuses": [status.value for status in CampaignStatus],
        "filters": {
            "status": status or "",
            "language": language or "",
            "has_reply": has_reply or "",
        },
    }
    return templates.TemplateResponse(request, "campaigns.html", context)


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(
    request: Request,
    campaign_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    row = (
        db.query(Campaign, Company, Contact)
        .join(Company, Company.id == Campaign.company_id)
        .outerjoin(Contact, Contact.id == Campaign.contact_id)
        .filter(Campaign.id == campaign_id)
    )
    if tenant_id is not None:
        row = row.filter(Company.tenant_id == tenant_id)
    row = row.first()
    if not row:
        raise HTTPException(status_code=404, detail="campaign_not_found")

    campaign, company, contact = row
    messages = db.query(Message).filter(Message.campaign_id == campaign.id).order_by(Message.created_at.asc()).all()
    schedules = db.query(Schedule).filter(Schedule.campaign_id == campaign.id).order_by(Schedule.run_at.asc()).all()
    handoffs = db.query(Handoff).filter(Handoff.campaign_id == campaign.id).order_by(Handoff.created_at.desc()).all()

    context = {
        **_base_context(request, f"Кампания #{campaign.id}"),
        "campaign": campaign,
        "company": company,
        "contact": contact,
        "messages": messages,
        "schedules": schedules,
        "handoffs": handoffs,
    }
    return templates.TemplateResponse(request, "campaign_detail.html", context)


@router.get("/messages", response_class=HTMLResponse)
def messages_page(
    request: Request,
    direction: str | None = None,
    sent_state: str | None = None,
    company: str | None = None,
    country: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = (
        db.query(Message, Campaign, Company, Contact)
        .join(Campaign, Campaign.id == Message.campaign_id)
        .join(Company, Company.id == Campaign.company_id)
        .outerjoin(Contact, Contact.id == Campaign.contact_id)
    )
    if tenant_id is not None:
        query = query.filter(Company.tenant_id == tenant_id)

    if direction:
        query = query.filter(Message.direction == direction)

    if sent_state == "sent":
        query = query.filter(or_(Message.sent_at.isnot(None), Message.received_at.isnot(None)))
    elif sent_state == "not_sent":
        query = query.filter(Message.direction == MessageDirection.outbound, Message.sent_at.is_(None))

    if company:
        like = f"%{company.strip()}%"
        query = query.filter(or_(Company.domain.ilike(like), Company.name.ilike(like)))

    if country:
        query = query.filter(Company.country == country)

    dt_from = _to_dt(date_from)
    dt_to = _to_dt(date_to, at_end_of_day=True)
    timeline_expr = func.coalesce(Message.sent_at, Message.received_at, Message.created_at)
    if dt_from:
        query = query.filter(timeline_expr >= dt_from)
    if dt_to:
        query = query.filter(timeline_expr <= dt_to)

    rows = query.order_by(desc(timeline_expr)).limit(400).all()

    countries_q = db.query(Company.country)
    if tenant_id is not None:
        countries_q = countries_q.filter(Company.tenant_id == tenant_id)
    countries = [row[0] for row in countries_q.filter(Company.country.isnot(None)).distinct().order_by(Company.country.asc()).all()]

    context = {
        **_base_context(request, "Сообщения"),
        "rows": rows,
        "countries": countries,
        "filters": {
            "direction": direction or "",
            "sent_state": sent_state or "",
            "company": company or "",
            "country": country or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
        },
    }
    return templates.TemplateResponse(request, "messages.html", context)


@router.get("/replies", response_class=HTMLResponse)
def replies_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = (
        db.query(Reply, Message, Campaign, Company, Contact)
        .join(Message, Message.id == Reply.message_id)
        .join(Campaign, Campaign.id == Message.campaign_id)
        .join(Company, Company.id == Campaign.company_id)
        .outerjoin(Contact, Contact.id == Campaign.contact_id)
        .filter(*( [Company.tenant_id == tenant_id] if tenant_id is not None else [] ))
        .order_by(Reply.created_at.desc())
        .limit(300)
        .all()
    )

    context = {
        **_base_context(request, "Ответы"),
        "rows": rows,
    }
    return templates.TemplateResponse(request, "replies.html", context)


@router.get("/handoffs", response_class=HTMLResponse)
def handoffs_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = (
        db.query(Handoff, Campaign, Company, Contact)
        .outerjoin(Campaign, Campaign.id == Handoff.campaign_id)
        .outerjoin(Company, Company.id == Handoff.company_id)
        .outerjoin(Contact, Contact.id == Handoff.contact_id)
        .filter(*( [Company.tenant_id == tenant_id] if tenant_id is not None else [] ))
        .order_by(Handoff.created_at.desc())
        .limit(300)
        .all()
    )

    context = {
        **_base_context(request, "Передачи"),
        "rows": rows,
    }
    return templates.TemplateResponse(request, "handoffs.html", context)


@router.get("/operations", response_class=HTMLResponse)
def operations_page(
    request: Request,
    auth_action: str | None = None,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    actions_map = {
        "finder": ["finder_run_requested", "plan_and_run_requested"],
        "planner": ["planner_preview", "plan_and_run_requested"],
        "research": ["site_researched", "company_qualified"],
        "outreach": ["outreach_sequence_generated", "outreach_skipped"],
        "send": ["email_sent", "send_deferred", "campaign_stopped"],
        "replies": ["reply_ingest_requested", "reply_classified", "reply_unmatched_manual_review"],
    }

    grouped: dict[str, list[AuditLog]] = {}
    for key, actions in actions_map.items():
        grouped[key] = (
            db.query(AuditLog)
            .filter(AuditLog.action.in_(actions))
            .order_by(AuditLog.created_at.desc())
            .limit(30)
            .all()
        )

    auth_actions = [
        "auth_login_blocked",
        "auth_login_failed",
        "auth_login_denied",
        "auth_login_success",
        "auth_logout",
        "auth_logout_all",
        "auth_resend_verification_requested",
        "auth_forgot_password_requested",
        "auth_password_reset_completed",
        "ui_auth_login_blocked",
        "ui_auth_login_failed",
        "ui_auth_login_denied",
        "ui_auth_login_success",
        "ui_auth_logout",
        "ui_auth_resend_verification_requested",
        "ui_auth_forgot_password_requested",
        "ui_auth_password_reset_completed",
    ]
    auth_query = db.query(AuditLog).filter(AuditLog.entity_type == "auth")
    if auth_action and auth_action in auth_actions:
        auth_query = auth_query.filter(AuditLog.action == auth_action)
    auth_rows = auth_query.order_by(AuditLog.created_at.desc()).limit(100).all()

    recent_tasks = db.query(Task).order_by(Task.created_at.desc()).limit(50).all()
    recent_audit = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(100).all()

    errors = [
        row
        for row in recent_audit
        if row.action
        in {
            "reply_unmatched_manual_review",
            "campaign_stopped",
            "outreach_skipped",
            "auth_login_failed",
            "auth_login_blocked",
            "ui_auth_login_failed",
            "ui_auth_login_blocked",
        }
    ]

    context = {
        **_base_context(request, "Операции"),
        "grouped": grouped,
        "recent_tasks": recent_tasks,
        "recent_audit": recent_audit,
        "errors": errors,
        "auth_rows": auth_rows,
        "auth_actions": auth_actions,
        "auth_action": auth_action or "",
        "json_excerpt": _json_excerpt,
    }
    return templates.TemplateResponse(request, "operations.html", context)


@router.get("/actions", response_class=HTMLResponse)
def actions_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recent_companies = _company_query(db, tenant_id).order_by(Company.created_at.desc()).limit(50).all()
    recent_campaigns = _campaign_query(db, tenant_id).order_by(Campaign.created_at.desc()).limit(50).all()

    context = {
        **_base_context(request, "Действия"),
        "recent_companies": recent_companies,
        "recent_campaigns": recent_campaigns,
    }
    return templates.TemplateResponse(request, "actions.html", context)


@router.get("/team", response_class=HTMLResponse)
def team_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    actor: TenantMembership = Depends(require_roles("viewer", "operator", "manager", "admin", "owner", strict=True)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if tenant_id is None:
        return _flash_redirect("/ui", error="Выберите tenant")

    rows = (
        db.query(TenantMembership, User)
        .join(User, User.id == TenantMembership.user_id)
        .filter(TenantMembership.tenant_id == tenant_id)
        .order_by(TenantMembership.id.asc())
        .all()
    )
    actor_role = str(getattr(actor.role, "value", actor.role))
    context = {
        **_base_context(request, "Команда"),
        "rows": rows,
        "roles": [x.value for x in MembershipRole],
        "statuses": ["active", "disabled"],
        "actor_role": actor_role,
        "latest_invite_token": request.query_params.get("invite_token"),
    }
    return templates.TemplateResponse(request, "team.html", context)


@router.post("/team/add")
def team_add_member(
    email: str = Form(...),
    full_name: str = Form(default=""),
    role: str = Form(default="operator"),
    password: str = Form(default=""),
    tenant_id: int | None = Depends(get_tenant_id),
    actor: TenantMembership = Depends(require_roles("admin", "owner", strict=True)),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if tenant_id is None:
        return _flash_redirect("/ui/team", error="Tenant не выбран")
    if actor.tenant_id != tenant_id:
        return _flash_redirect("/ui/team", error="Недостаточно прав")

    clean_role = (role or "operator").strip().lower()
    if clean_role not in {x.value for x in MembershipRole}:
        return _flash_redirect("/ui/team", error="Некорректная роль")

    clean_email = (email or "").strip().lower()
    if not clean_email:
        return _flash_redirect("/ui/team", error="Email обязателен")

    user = db.query(User).filter(User.email == clean_email).first()
    if user is None:
        if not password or len(password) < 8:
            return _flash_redirect("/ui/team", error="Для нового пользователя нужен пароль не короче 8 символов")
        user = User(
            email=clean_email,
            password_hash=hash_password(password),
            full_name=_trim(full_name),
            is_active=True,
            email_verified=False,
        )
        db.add(user)
        db.flush()
    elif _trim(full_name) and not user.full_name:
        user.full_name = _trim(full_name)
        db.add(user)

    membership = (
        db.query(TenantMembership)
        .filter(TenantMembership.tenant_id == tenant_id, TenantMembership.user_id == user.id)
        .first()
    )
    if membership:
        membership.role = MembershipRole(clean_role)
        membership.status = "active"
        db.add(membership)
        db.commit()
        return _flash_redirect("/ui/team", message="Участник обновлен")

    membership = TenantMembership(
        tenant_id=tenant_id,
        user_id=user.id,
        role=MembershipRole(clean_role),
        status="active",
    )
    db.add(membership)
    db.commit()
    return _flash_redirect("/ui/team", message="Участник добавлен")


@router.post("/team/{membership_id}/update")
def team_update_member(
    membership_id: int,
    role: str = Form(...),
    status: str = Form(...),
    tenant_id: int | None = Depends(get_tenant_id),
    actor: TenantMembership = Depends(require_roles("admin", "owner", strict=True)),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if tenant_id is None:
        return _flash_redirect("/ui/team", error="Tenant не выбран")
    if actor.tenant_id != tenant_id:
        return _flash_redirect("/ui/team", error="Недостаточно прав")

    target = (
        db.query(TenantMembership)
        .filter(TenantMembership.id == membership_id, TenantMembership.tenant_id == tenant_id)
        .first()
    )
    if not target:
        return _flash_redirect("/ui/team", error="Участник не найден")

    actor_role = str(getattr(actor.role, "value", actor.role))
    target_role = str(getattr(target.role, "value", target.role))

    clean_role = (role or "").strip().lower()
    clean_status = (status or "").strip().lower()
    if clean_role not in {x.value for x in MembershipRole}:
        return _flash_redirect("/ui/team", error="Некорректная роль")
    if clean_status not in {"active", "disabled"}:
        return _flash_redirect("/ui/team", error="Некорректный статус")
    if actor_role != "owner" and (target_role == "owner" or clean_role == "owner"):
        return _flash_redirect("/ui/team", error="Только owner может управлять owner")

    target.role = MembershipRole(clean_role)
    target.status = clean_status
    db.add(target)
    db.commit()
    return _flash_redirect("/ui/team", message="Права участника обновлены")


@router.post("/team/invite")
def team_invite_member(
    email: str = Form(...),
    role: str = Form(default="operator"),
    expires_in_hours: int = Form(default=72),
    tenant_id: int | None = Depends(get_tenant_id),
    actor: TenantMembership = Depends(require_roles("admin", "owner", strict=True)),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if tenant_id is None:
        return _flash_redirect("/ui/team", error="Tenant не выбран")
    if actor.tenant_id != tenant_id:
        return _flash_redirect("/ui/team", error="Недостаточно прав")

    clean_role = (role or "").strip().lower()
    if clean_role not in {x.value for x in MembershipRole}:
        return _flash_redirect("/ui/team", error="Некорректная роль")
    if expires_in_hours < 1 or expires_in_hours > 336:
        return _flash_redirect("/ui/team", error="Срок invite должен быть от 1 до 336 часов")

    clean_email = (email or "").strip().lower()
    if not clean_email:
        return _flash_redirect("/ui/team", error="Email обязателен")

    pending = (
        db.query(TenantInvite)
        .filter(TenantInvite.tenant_id == tenant_id, TenantInvite.email == clean_email, TenantInvite.status == "pending")
        .first()
    )
    if pending:
        return _flash_redirect("/ui/team", error="Для этого email уже есть активный invite")

    _, invite_token = create_invite(
        db,
        tenant_id=tenant_id,
        invited_by_user_id=actor.user_id,
        email=clean_email,
        role=clean_role,
        expires_in_hours=expires_in_hours,
    )

    tenant_name = actor.tenant.name if actor.tenant else "workspace"
    base_url = settings.app_public_base_url.rstrip("/")
    invite_url = f"{base_url}/ui/accept-invite?token={invite_token}"

    try:
        send_invite_email(
            to_email=clean_email,
            invite_url=invite_url,
            tenant_name=tenant_name,
            role=clean_role,
        )
        return _flash_redirect("/ui/team", message=f"Invite отправлен на {clean_email}")
    except Exception as exc:
        # Fallback for local/dev mode where SMTP may be unavailable.
        return _flash_redirect(
            f"/ui/team?invite_token={invite_token}",
            error=f"SMTP недоступен ({exc}). Используйте invite token вручную.",
        )


@router.post("/companies/{company_id}/research-qualify")
def company_action_research_qualify(
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    company = _company_by_id(db, company_id, tenant_id)
    if not company:
        return _flash_redirect("/ui/companies", error="Компания не найдена")

    task = run_research_and_qualify.delay(company_id=company_id, tenant_id=tenant_id)
    db.add(
        AuditLog(
            entity_type="company",
            entity_id=company_id,
            action="ui_research_qualify_requested",
            details={"task_id": task.id},
            reason="Requested from UI company page.",
        )
    )
    db.commit()
    return _flash_redirect(f"/ui/companies/{company_id}", message=f"Research+qualify запланирован: {task.id}")


@router.post("/companies/{company_id}/outreach/generate")
def company_action_generate_outreach(
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    company = _company_by_id(db, company_id, tenant_id)
    if not company:
        return _flash_redirect("/ui/companies", error="Компания не найдена")

    task = generate_outreach_for_company.delay(company_id=company_id, tenant_id=tenant_id)
    db.add(
        AuditLog(
            entity_type="company",
            entity_id=company_id,
            action="ui_outreach_generate_requested",
            details={"task_id": task.id},
            reason="Requested from UI.",
        )
    )
    db.commit()
    return _flash_redirect(f"/ui/companies/{company_id}", message=f"Генерация outreach запланирована: {task.id}")


@router.post("/companies/{company_id}/blacklist-domain")
def company_action_blacklist_domain(
    company_id: int,
    reason: str = Form(default="Ручной blacklist из UI"),
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    company = _company_by_id(db, company_id, tenant_id)
    if not company:
        return _flash_redirect("/ui/companies", error="Компания не найдена")

    existing = (
        db.query(Blacklist)
        .filter(Blacklist.entry_type == "domain", Blacklist.value == company.domain)
        .first()
    )
    if existing:
        return _flash_redirect(f"/ui/companies/{company_id}", message="Домен уже в blacklist")

    db.add(Blacklist(entry_type="domain", value=company.domain, reason=_trim(reason)))
    db.add(
        AuditLog(
            entity_type="company",
            entity_id=company_id,
            action="domain_blacklisted",
            details={"domain": company.domain},
            reason="Manual UI action.",
        )
    )
    db.commit()
    return _flash_redirect(f"/ui/companies/{company_id}", message="Домен добавлен в blacklist")


@router.post("/companies/{company_id}/blacklist-contact")
def company_action_blacklist_contact(
    company_id: int,
    email: str = Form(...),
    reason: str = Form(default="Ручной blacklist из UI"),
    _membership=Depends(require_roles("manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    clean_email = _trim(email)
    if not clean_email:
        return _flash_redirect(f"/ui/companies/{company_id}", error="Нужно указать email")

    existing = (
        db.query(Blacklist)
        .filter(Blacklist.entry_type == "email", Blacklist.value == clean_email.lower())
        .first()
    )
    if existing:
        return _flash_redirect(f"/ui/companies/{company_id}", message="Email уже в blacklist")

    db.add(Blacklist(entry_type="email", value=clean_email.lower(), reason=_trim(reason)))
    db.add(
        AuditLog(
            entity_type="company",
            entity_id=company_id,
            action="email_blacklisted",
            details={"email": clean_email.lower()},
            reason="Manual UI action.",
        )
    )
    db.commit()
    return _flash_redirect(f"/ui/companies/{company_id}", message="Контакт добавлен в blacklist")


@router.post("/companies/{company_id}/refresh-contacts")
def company_action_refresh_contacts(
    company_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    company = _company_by_id(db, company_id, tenant_id)
    if not company:
        return _flash_redirect("/ui/companies", error="Компания не найдена")

    resolved = resolve_contacts_for_company(company_id=company_id, db=db)
    created = save_contacts(company_id=company_id, contacts=resolved, db=db)

    db.add(
        AuditLog(
            entity_type="company",
            entity_id=company_id,
            action="contacts_refreshed",
            details={"resolved": len(resolved), "created": len(created)},
            reason="Manual refresh contacts from UI.",
        )
    )
    db.commit()
    return _flash_redirect(f"/ui/companies/{company_id}", message=f"Контакты обновлены: +{len(created)}")


@router.post("/campaigns/{campaign_id}/send-now")
def campaign_action_send_now(
    campaign_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    campaign = _campaign_by_id(db, campaign_id, tenant_id)
    if not campaign:
        return _flash_redirect("/ui/campaigns", error="Кампания не найдена")

    task = send_campaign_step.delay(campaign_id=campaign_id)
    db.add(
        AuditLog(
            entity_type="campaign",
            entity_id=campaign_id,
            action="ui_send_now_requested",
            details={"task_id": task.id},
            reason="Manual send request from UI.",
        )
    )
    db.commit()
    return _flash_redirect(f"/ui/campaigns/{campaign_id}", message=f"Отправка запланирована: {task.id}")


@router.post("/campaigns/{campaign_id}/stop")
def campaign_action_stop(
    campaign_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    campaign = _campaign_by_id(db, campaign_id, tenant_id)
    if not campaign:
        return _flash_redirect("/ui/campaigns", error="Кампания не найдена")

    campaign.status = CampaignStatus.stopped
    db.add(campaign)
    db.add(
        AuditLog(
            entity_type="campaign",
            entity_id=campaign_id,
            action="campaign_stopped",
            details={"source": "ui"},
            reason="Manually stopped from UI.",
        )
    )
    db.commit()
    return _flash_redirect(f"/ui/campaigns/{campaign_id}", message="Кампания остановлена")


@router.post("/campaigns/{campaign_id}/mark-replied")
def campaign_action_mark_replied(
    campaign_id: int,
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    campaign = _campaign_by_id(db, campaign_id, tenant_id)
    if not campaign:
        return _flash_redirect("/ui/campaigns", error="Кампания не найдена")

    campaign.has_reply = True
    campaign.status = CampaignStatus.replied
    db.add(campaign)
    db.add(
        AuditLog(
            entity_type="campaign",
            entity_id=campaign_id,
            action="campaign_marked_replied",
            details={"source": "ui"},
            reason="Manually marked as replied from UI.",
        )
    )
    db.commit()
    return _flash_redirect(f"/ui/campaigns/{campaign_id}", message="Кампания отмечена как replied")


@router.post("/handoffs/{handoff_id}/status")
def handoff_action_update_status(
    handoff_id: int,
    status: str = Form(...),
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    handoff = _handoff_query(db, tenant_id).filter(Handoff.id == handoff_id).first()
    if not handoff:
        return _flash_redirect("/ui/handoffs", error="Handoff не найден")

    handoff.status = status
    db.add(handoff)
    db.add(
        AuditLog(
            entity_type="handoff",
            entity_id=handoff_id,
            action="handoff_status_updated",
            details={"status": status},
            reason="Updated from UI.",
        )
    )
    db.commit()
    return _flash_redirect("/ui/handoffs", message="Статус handoff обновлен")


@router.post("/actions/finder-run")
def actions_run_finder(
    countries: str = Form(...),
    keywords: str = Form(default=""),
    results_per_query: int = Form(default=10),
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    countries_list = [part.strip() for part in countries.split(",") if part.strip()]
    keywords_list = [part.strip() for part in keywords.split(",") if part.strip()]
    if not countries_list:
        return _flash_redirect("/ui/actions", error="Нужно указать хотя бы одну страну")

    task = run_finder.delay(
        countries=countries_list,
        keywords=keywords_list or None,
        results_per_query=results_per_query,
        tenant_id=tenant_id,
    )
    db.add(
        AuditLog(
            entity_type="ui",
            entity_id=None,
            action="finder_run_requested",
            details={
                "task_id": task.id,
                "countries": countries_list,
                "keywords": keywords_list,
                "results_per_query": results_per_query,
            },
            reason="Triggered from Actions page.",
        )
    )
    db.commit()
    return _flash_redirect("/ui/actions", message=f"Finder запланирован: {task.id}")


@router.post("/actions/planner-preview")
def actions_planner_preview(
    intent: str = Form(...),
    country: str = Form(default="Lithuania"),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        plan = generate_search_plan(intent, country=country)
    except ValueError as exc:
        return _flash_redirect("/ui/actions", error=str(exc))

    db.add(
        AuditLog(
            entity_type="ui",
            entity_id=None,
            action="planner_preview",
            details={
                "country": plan.country,
                "industries": plan.priority_industries,
                "en_queries": len(plan.search_queries_en),
                "lt_queries": len(plan.search_queries_lt),
                "sample_en": plan.search_queries_en[:3],
                "sample_lt": plan.search_queries_lt[:3],
            },
            reason="Planner preview from Actions page.",
        )
    )
    db.commit()
    return _flash_redirect("/ui/actions", message="Preview planner сохранен в operations log")


@router.post("/actions/plan-and-run")
def actions_plan_and_run(
    intent: str = Form(...),
    country: str = Form(default="Lithuania"),
    results_per_query: int = Form(default=20),
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        plan = generate_search_plan(intent, country=country)
    except ValueError as exc:
        return _flash_redirect("/ui/actions", error=str(exc))

    task = run_finder_with_plan.delay(
        intent=intent,
        country=country,
        results_per_query=results_per_query,
        tenant_id=tenant_id,
    )
    db.add(
        AuditLog(
            entity_type="ui",
            entity_id=None,
            action="plan_and_run_requested",
            details={
                "task_id": task.id,
                "country": plan.country,
                "results_per_query": results_per_query,
                "en_queries": len(plan.search_queries_en),
                "lt_queries": len(plan.search_queries_lt),
                "industries": plan.priority_industries,
            },
            reason="Plan and run from Actions page.",
        )
    )
    db.commit()
    return _flash_redirect("/ui/actions", message=f"Plan+Finder запланирован: {task.id}")


@router.post("/actions/ingest-replies")
def actions_ingest_replies(
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    task = ingest_and_classify.delay()
    db.add(
        AuditLog(
            entity_type="ui",
            entity_id=None,
            action="reply_ingest_requested",
            details={"task_id": task.id},
            reason="Manual ingest from Actions page.",
        )
    )
    db.commit()
    return _flash_redirect("/ui/actions", message=f"Ingest ответов запланирован: {task.id}")


@router.post("/actions/process-due")
def actions_process_due_mail(
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    task = process_due_schedules.delay()
    db.add(
        AuditLog(
            entity_type="ui",
            entity_id=None,
            action="process_due_requested",
            details={"task_id": task.id},
            reason="Manual process-due from Actions page.",
        )
    )
    db.commit()
    return _flash_redirect("/ui/actions", message=f"Process due запланирован: {task.id}")


@router.post("/actions/health-check")
def actions_health_check(
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
) -> RedirectResponse:
    try:
        result = check_zone_connectivity()
        return _flash_redirect(
            "/ui/actions",
            message=f"Health ОК: smtp={result.smtp_ok}, imap={result.imap_ok}",
        )
    except Exception as exc:
        return _flash_redirect("/ui/actions", error=f"Проверка health завершилась ошибкой: {exc}")


@router.post("/actions/research-qualify")
def actions_research_qualify(
    company_id: int = Form(...),
    tenant_id: int | None = Depends(get_tenant_id),
    _membership=Depends(require_roles("operator", "manager", "admin", "owner")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    company = _company_by_id(db, company_id, tenant_id)
    if not company:
        return _flash_redirect("/ui/actions", error="Компания не найдена")

    task = run_research_and_qualify.delay(company_id=company_id, tenant_id=tenant_id)
    db.add(
        AuditLog(
            entity_type="company",
            entity_id=company_id,
            action="ui_research_qualify_requested",
            details={"task_id": task.id},
            reason="Requested from Actions page.",
        )
    )
    db.commit()
    return _flash_redirect("/ui/actions", message=f"Research+qualify запланирован: {task.id}")


@router.get("/accept-invite", response_class=HTMLResponse)
def accept_invite_page(request: Request, token: str | None = None) -> HTMLResponse:
    context = {
        **_base_context(request, "Принять приглашение"),
        "token": token or "",
    }
    return templates.TemplateResponse(request, "accept_invite.html", context)


@router.post("/accept-invite")
def accept_invite_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(default=""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    payload = AcceptInviteRequest(token=token, password=password, full_name=_trim(full_name))
    try:
        session_data = api_accept_invite(payload=payload, request=request, db=db)
    except HTTPException as exc:
        return _flash_redirect(f"/ui/accept-invite?token={token}", error=str(exc.detail))
    response = _flash_redirect(
        "/ui",
        message=f"Приглашение принято. Аккаунт активирован для tenant: {session_data.tenant_slug}.",
    )
    response.set_cookie(
        key=settings.auth_session_cookie_name,
        value=session_data.token,
        httponly=True,
        secure=settings.auth_session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", _base_context(request, "Вход"))


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    tenant_slug: str = Form(default=""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from sqlalchemy.orm import joinedload as _joinedload
    from app.services.auth.session_manager import pick_membership, create_session as _create_session

    normalized_email = email.strip().lower()
    if not is_login_allowed(email=normalized_email):
        _audit_auth_event(
            db,
            action="ui_auth_login_blocked",
            reason="login_temporarily_locked",
            details={"email": normalized_email},
        )
        return _flash_redirect("/ui/login", error="Слишком много попыток входа. Попробуйте позже.")

    user = db.query(User).filter(User.email == normalized_email).first()
    if not user or not verify_password(password, user.password_hash):
        locked_now = register_login_failure(
            email=normalized_email,
            max_attempts=settings.auth_login_max_attempts,
            window_seconds=settings.auth_login_attempt_window_seconds,
            lockout_seconds=settings.auth_login_lockout_seconds,
        )
        if locked_now:
            _audit_auth_event(
                db,
                action="ui_auth_login_blocked",
                reason="login_lock_threshold_reached",
                details={"email": normalized_email},
            )
            return _flash_redirect("/ui/login", error="Слишком много попыток входа. Попробуйте позже.")
        _audit_auth_event(
            db,
            action="ui_auth_login_failed",
            reason="invalid_credentials",
            details={"email": normalized_email},
        )
        return _flash_redirect("/ui/login", error="Неверный email или пароль")

    clear_login_failures(email=normalized_email)
    if not user.is_active:
        _audit_auth_event(
            db,
            action="ui_auth_login_denied",
            user_id=user.id,
            reason="user_inactive",
            details={"email": normalized_email},
        )
        return _flash_redirect("/ui/login", error="Аккаунт деактивирован")
    if settings.auth_require_email_verified and not user.email_verified:
        _audit_auth_event(
            db,
            action="ui_auth_login_denied",
            user_id=user.id,
            reason="email_not_verified",
            details={"email": normalized_email},
        )
        return _flash_redirect(
            "/ui/login",
            error="Подтвердите email перед входом. Если письмо не пришло, используйте форму восстановления пароля.",
        )

    memberships = (
        db.query(TenantMembership)
        .options(_joinedload(TenantMembership.tenant))
        .filter(TenantMembership.user_id == user.id)
        .all()
    )
    membership = pick_membership(memberships, tenant_slug.strip() or None)
    if membership is None:
        _audit_auth_event(
            db,
            action="ui_auth_login_denied",
            user_id=user.id,
            reason="membership_not_found_or_ambiguous",
            details={"email": normalized_email},
        )
        return _flash_redirect("/ui/login", error="Workspace не найден. Укажите правильный slug.")

    _, token = _create_session(
        db,
        user_id=user.id,
        tenant_id=membership.tenant_id,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _audit_auth_event(
        db,
        action="ui_auth_login_success",
        user_id=user.id,
        details={"tenant_id": membership.tenant_id, "email": normalized_email},
    )
    response = _flash_redirect("/ui", message="Добро пожаловать!")
    response.set_cookie(
        key=settings.auth_session_cookie_name,
        value=token,
        httponly=True,
        secure=settings.auth_session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/resend-verification")
def resend_verification_submit(
    email: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    normalized_email = email.strip().lower()
    user = db.query(User).filter(User.email == normalized_email).first()
    sent = False
    if user and user.is_active and not user.email_verified:
        can_send = acquire_email_cooldown(
            purpose="resend-verify",
            email=normalized_email,
            ttl_seconds=settings.auth_resend_verification_cooldown_seconds,
        )

        if can_send:
            raw_token = create_email_token(db, user_id=user.id, purpose="verify_email")
            verify_url = f"{settings.app_public_base_url}/ui/verify-email?token={raw_token}"
            try:
                send_verification_email(to_email=user.email, verify_url=verify_url)
                sent = True
            except Exception:
                pass
    _audit_auth_event(
        db,
        action="ui_auth_resend_verification_requested",
        user_id=user.id if user else None,
        details={"email": normalized_email, "sent": sent},
    )
    return _flash_redirect(
        "/ui/login",
        message="Если адрес зарегистрирован, письмо подтверждения отправлено.",
    )


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "register.html", _base_context(request, "Регистрация"))


@router.post("/register")
def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(default=""),
    tenant_name: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.api.routes.auth import register as api_register
    from app.schemas.auth import RegisterRequest

    payload = RegisterRequest(
        email=email.strip(),
        password=password,
        full_name=_trim(full_name),
        tenant_name=tenant_name.strip(),
    )
    try:
        session_data = api_register(payload=payload, request=request, db=db)
    except HTTPException as exc:
        return _flash_redirect("/ui/register", error=str(exc.detail))

    response = _flash_redirect("/ui", message="Регистрация успешна! Проверьте email для подтверждения адреса.")
    response.set_cookie(
        key=settings.auth_session_cookie_name,
        value=session_data.token,
        httponly=True,
        secure=settings.auth_session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

@router.get("/verify-email")
def verify_email_page(
    request: Request,
    token: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if not token:
        return _flash_redirect("/ui", error="Токен не указан")
    row = consume_email_token(db, token, "verify_email")
    if row is None:
        return _flash_redirect("/ui/login", error="Ссылка подтверждения недействительна или истекла")
    user = db.query(User).filter(User.id == row.user_id).first()
    if user:
        user.email_verified = True
        db.commit()
    return _flash_redirect("/ui", message="Email подтверждён! Вы можете войти.")


# ---------------------------------------------------------------------------
# Forgot / Reset password
# ---------------------------------------------------------------------------

@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "forgot_password.html", _base_context(request, "Восстановление пароля"))


@router.post("/forgot-password")
def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    normalized_email = email.strip().lower()
    user = db.query(User).filter(User.email == normalized_email).first()
    sent = False
    if user and user.is_active:
        can_send = acquire_email_cooldown(
            purpose="forgot-password",
            email=normalized_email,
            ttl_seconds=settings.auth_forgot_password_cooldown_seconds,
        )

        if can_send:
            raw_token = create_email_token(db, user_id=user.id, purpose="reset_password")
            reset_url = f"{settings.app_public_base_url}/ui/reset-password?token={raw_token}"
            try:
                send_password_reset_email(to_email=user.email, reset_url=reset_url)
                sent = True
            except Exception:
                pass
    _audit_auth_event(
        db,
        action="ui_auth_forgot_password_requested",
        user_id=user.id if user else None,
        details={"email": normalized_email, "sent": sent},
    )
    return _flash_redirect(
        "/ui/forgot-password",
        message="Если адрес зарегистрирован, письмо отправлено.",
    )


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str | None = None) -> HTMLResponse:
    context = {**_base_context(request, "Новый пароль"), "token": token or ""}
    return templates.TemplateResponse(request, "reset_password.html", context)


@router.post("/reset-password")
def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    row = consume_email_token(db, token, "reset_password")
    if row is None:
        return _flash_redirect(f"/ui/reset-password?token={token}", error="Ссылка недействительна или истекла")
    user = db.query(User).filter(User.id == row.user_id).first()
    if user is None:
        return _flash_redirect("/ui/login", error="Пользователь не найден")
    user.password_hash = hash_password(password)
    db.commit()
    _audit_auth_event(
        db,
        action="ui_auth_password_reset_completed",
        user_id=user.id,
        details={"email": user.email},
    )
    return _flash_redirect("/ui/login", message="Пароль изменён. Войдите с новым паролем.")

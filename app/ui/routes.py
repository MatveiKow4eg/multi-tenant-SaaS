from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path
import re
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
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
from app.models.tenant import Tenant
from app.models.tenant_membership import MembershipRole, TenantMembership
from app.models.tenant_invite import TenantInvite
from app.models.user import User
from app.models.user_session import UserSession
from app.models.email_token import EmailToken
from app.models.sender_domain import ManagedDkimSelector, SenderDomain
from app.services.sender_domains import (
    authentication_status,
    create_sender_domain_profile,
    ensure_ownership_token,
    mark_sender_domain_ownership_email_pending,
    mark_sender_domain_ownership_email_verified,
    normalize_domain,
    rotate_dkim,
    verify_sender_domain,
    verify_sender_domain_ownership_dns,
)
from app.services.auth.security import hash_password, verify_password
from app.services.auth.email_tokens import create_email_token, consume_email_token
from app.services.auth.rate_limit import acquire_email_cooldown, clear_login_failures, is_login_allowed, register_login_failure
from app.services.mail.auth_emails import send_verification_email, send_password_reset_email
from app.services.mail.ownership_emails import send_domain_ownership_verification_email
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
from app.core.feature_toggles import parse_feature_toggles
from app.core.feature_toggles import active_feature_toggles, parse_feature_toggles
from app.utils.logger_factory import get_logger

logger = get_logger("app.ui.routes")

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


def _extract_domain_from_website(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None

    candidate = raw
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    try:
        parsed = urlparse(candidate)
    except Exception:
        return None

    host = (parsed.hostname or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None

    try:
        return normalize_domain(host)
    except Exception:
        return None


def _resolve_request_user(request: Request, db: Session, tenant_id: int | None) -> User | None:
    token = request.cookies.get(settings.auth_session_cookie_name)
    if not token:
        return None
    session = resolve_active_session(db, token)
    if session is None:
        return None
    if tenant_id is not None and session.tenant_id != tenant_id:
        return None
    return db.query(User).filter(User.id == session.user_id).first()


def _extract_email_domain(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    match = re.fullmatch(r"[^@\s]+@([^@\s]+)", candidate)
    if not match:
        return None
    return (match.group(1) or "").strip().lower()


def _domain_ownership_email_token_purpose(domain_id: int) -> str:
    return f"verify_domain_{domain_id}"


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
    if path.startswith("/ui/lists"):
        return "lists"
    if path.startswith("/ui/companies"):
        return "companies"
    if path.startswith("/ui/campaigns"):
        return "campaigns"
    if path.startswith("/ui/analytics"):
        return "analytics"
    if path.startswith("/ui/settings"):
        return "settings"
    if path.startswith("/ui/feature-toggles"):
        return "settings"
    if path.startswith("/ui/billing"):
        return "billing"
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
    if path.startswith("/ui/domains"):
        return "domains"
    return ""


def _raise_team_ui_disabled() -> None:
    raise HTTPException(status_code=404, detail="team_ui_disabled")


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


def _last_n_day_labels(days: int) -> list[str]:
    today = datetime.utcnow().date()
    return [
        (today - timedelta(days=offset)).strftime("%d %b")
        for offset in range(days - 1, -1, -1)
    ]


def _bucket_datetimes_by_day(values: list[datetime | None], days: int) -> list[int]:
    today = datetime.utcnow().date()
    start_day = today - timedelta(days=days - 1)
    counts = Counter(
        value.date()
        for value in values
        if value is not None and value.date() >= start_day
    )
    return [counts.get(start_day + timedelta(days=index), 0) for index in range(days)]


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
    tenant_hint = request.headers.get("X-Tenant-Id") or request.query_params.get("tenant_id") or "global"
    return {
        "request": request,
        "page_title": page_title,
        "flash_message": request.query_params.get("msg"),
        "flash_error": request.query_params.get("err"),
        "now": datetime.utcnow(),
        "active_nav": _nav_key(request.url.path),
        "tenant_hint": tenant_hint,
        "workspace_label": f"tenant:{tenant_hint}",
        "csrf_cookie_name": settings.auth_csrf_cookie_name,
    }


def _audit_auth_event(
    db: Session,
    *,
    action: str,
    user_id: int | None = None,
    tenant_id: int | None = None,
    reason: str | None = None,
    details: dict | None = None,
) -> None:
    try:
        payload = dict(details or {})
        if tenant_id is not None:
            payload.setdefault("tenant_id", tenant_id)
        db.add(
            AuditLog(
                entity_type="auth",
                entity_id=user_id,
                tenant_id=tenant_id,
                action=action,
                details=payload or None,
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
                tenant_id=session.tenant_id,
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


def _tenant_user_rows_query(db: Session, tenant_id: int | None) -> list[User]:
    q = db.query(TenantMembership, User).join(User, User.id == TenantMembership.user_id)
    if tenant_id is not None:
        q = q.filter(TenantMembership.tenant_id == tenant_id)
    return q


def _all_tenant_user_rows_query(db: Session):
    """Returns (TenantMembership, User, Tenant) across every tenant."""
    return (
        db.query(TenantMembership, User, Tenant)
        .join(User, User.id == TenantMembership.user_id)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
    )


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


def _contact_query(db: Session, tenant_id: int | None, legacy_global: bool = False):
    q = db.query(Contact).join(Company, Company.id == Contact.company_id)
    if legacy_global:
        q = q.filter(Company.tenant_id.is_(None))
    elif tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    return q


def _dashboard_company_query(db: Session, tenant_id: int | None, legacy_global: bool = False):
    q = db.query(Company)
    if legacy_global:
        q = q.filter(Company.tenant_id.is_(None))
    elif tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    return q


def _dashboard_campaign_query(db: Session, tenant_id: int | None, legacy_global: bool = False):
    q = db.query(Campaign).join(Company, Company.id == Campaign.company_id)
    if legacy_global:
        q = q.filter(Company.tenant_id.is_(None))
    elif tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    return q


def _dashboard_message_query(db: Session, tenant_id: int | None, legacy_global: bool = False):
    q = db.query(Message).join(Campaign, Campaign.id == Message.campaign_id).join(Company, Company.id == Campaign.company_id)
    if legacy_global:
        q = q.filter(Company.tenant_id.is_(None))
    elif tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    return q


def _dashboard_handoff_query(db: Session, tenant_id: int | None, legacy_global: bool = False):
    q = db.query(Handoff).join(Company, Company.id == Handoff.company_id)
    if legacy_global:
        q = q.filter(Company.tenant_id.is_(None))
    elif tenant_id is not None:
        q = q.filter(Company.tenant_id == tenant_id)
    return q


def _dashboard_audit_query(db: Session, tenant_id: int | None, legacy_global: bool = False):
    q = db.query(AuditLog)
    if legacy_global:
        q = q.filter(AuditLog.tenant_id.is_(None))
    elif tenant_id is not None:
        q = q.filter(AuditLog.tenant_id == tenant_id)
    return q


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    flash_message = (request.query_params.get("msg") or "").strip()
    if flash_message.lower().startswith("onboarding"):
        return RedirectResponse(url="/ui", status_code=303)

    legacy_global_scope = False
    companies_q = _dashboard_company_query(db, tenant_id)
    status_counts_rows = companies_q.with_entities(Company.status, func.count(Company.id)).group_by(Company.status).all()
    status_counts = {_normalize_status(status): count for status, count in status_counts_rows}

    company_total = companies_q.count()
    contact_total = _contact_query(db, tenant_id).count()
    active_campaigns = (
        _dashboard_campaign_query(db, tenant_id)
        .filter(Campaign.status == CampaignStatus.active)
        .count()
    )
    message_q = _dashboard_message_query(db, tenant_id)
    sent_messages = (
        message_q.filter(Message.direction == MessageDirection.outbound, Message.sent_at.isnot(None)).count()
    )
    replies_count = (
        message_q.filter(Message.direction == MessageDirection.inbound).count()
    )
    handoff_q = _dashboard_handoff_query(db, tenant_id)
    warm_handoffs = (
        handoff_q.filter(Handoff.needs_human.is_(True)).count()
    )

    audit_q = _dashboard_audit_query(db, tenant_id)
    recent_activity = audit_q.order_by(AuditLog.created_at.desc()).limit(25).all()

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

    chart_days = 7
    activity_points = [row.created_at for row in audit_q.filter(AuditLog.created_at.isnot(None)).all()]
    sent_points = [
        row[0]
        for row in message_q
        .filter(Message.direction == MessageDirection.outbound, Message.sent_at.isnot(None))
        .with_entities(Message.sent_at)
        .all()
    ]
    reply_points = [
        row[0]
        for row in message_q
        .filter(Message.direction == MessageDirection.inbound)
        .with_entities(func.coalesce(Message.received_at, Message.created_at))
        .all()
    ]
    chart_labels = _last_n_day_labels(chart_days)
    dashboard_charts = {
        "labels": chart_labels,
        "activity": _bucket_datetimes_by_day(activity_points, chart_days),
        "pipeline": [
            status_counts.get("new", 0),
            status_counts.get("qualified", 0),
            status_counts.get("rejected", 0),
        ],
        "campaignTrend": {
            "sent": _bucket_datetimes_by_day(sent_points, chart_days),
            "replies": _bucket_datetimes_by_day(reply_points, chart_days),
        },
    }

    activity_rows = [
        {
            "row": row,
            "level": _event_level(row.action),
            "details_full": str(row.details) if row.details else "-",
        }
        for row in recent_activity
    ]

    context = {
        **_base_context(request, "Dashboard"),
        "company_total": company_total,
        "contact_total": contact_total,
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
        "dashboard_charts": dashboard_charts,
        "dashboard_uses_legacy_global": legacy_global_scope,
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
                tenant_id=company.tenant_id,
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
                tenant_id=company.tenant_id,
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
                tenant_id=company.tenant_id,
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
                        tenant_id=company.tenant_id,
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
                        tenant_id=company.tenant_id,
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
                    tenant_id=company.tenant_id,
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


@router.get("/lists", response_class=HTMLResponse)
def lists_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    company_total_q = db.query(func.count(Company.id))
    contact_total_q = db.query(func.count(Contact.id)).join(Company, Company.id == Contact.company_id)
    campaign_total_q = db.query(func.count(Campaign.id)).join(Company, Company.id == Campaign.company_id)
    if tenant_id is not None:
        company_total_q = company_total_q.filter(Company.tenant_id == tenant_id)
        contact_total_q = contact_total_q.filter(Company.tenant_id == tenant_id)
        campaign_total_q = campaign_total_q.filter(Company.tenant_id == tenant_id)

    company_total = company_total_q.scalar() or 0
    contact_total = contact_total_q.scalar() or 0
    campaign_total = campaign_total_q.scalar() or 0

    sample_lists = [
        {"name": "All companies", "type": "companies", "records": company_total, "source": "saved view"},
        {"name": "Contacts for outreach", "type": "contacts", "records": contact_total, "source": "resolver"},
        {"name": "Campaign-ready", "type": "mixed", "records": campaign_total, "source": "campaigns"},
    ]

    context = {
        **_base_context(request, "Lists"),
        "sample_lists": sample_lists,
    }
    return templates.TemplateResponse(request, "lists.html", context)


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    campaign_q = db.query(Campaign).join(Company, Company.id == Campaign.company_id)
    message_q = db.query(Message).join(Campaign, Campaign.id == Message.campaign_id).join(Company, Company.id == Campaign.company_id)
    reply_q = db.query(Reply).join(Message, Message.id == Reply.message_id).join(Campaign, Campaign.id == Message.campaign_id).join(Company, Company.id == Campaign.company_id)
    if tenant_id is not None:
        campaign_q = campaign_q.filter(Company.tenant_id == tenant_id)
        message_q = message_q.filter(Company.tenant_id == tenant_id)
        reply_q = reply_q.filter(Company.tenant_id == tenant_id)

    total_campaigns = campaign_q.count()
    sent_messages = message_q.filter(Message.direction == MessageDirection.outbound, Message.sent_at.isnot(None)).count()
    inbound_messages = message_q.filter(Message.direction == MessageDirection.inbound).count()
    total_replies = reply_q.count()
    active_campaigns = campaign_q.filter(Campaign.status == CampaignStatus.active).count()
    reply_rate = round((total_replies / sent_messages) * 100, 2) if sent_messages else 0.0

    top_campaigns = campaign_q.order_by(Campaign.updated_at.desc()).limit(10).all()

    context = {
        **_base_context(request, "Analytics"),
        "kpi": {
            "campaigns": total_campaigns,
            "active": active_campaigns,
            "sent": sent_messages,
            "inbound": inbound_messages,
            "replies": total_replies,
            "reply_rate": reply_rate,
        },
        "top_campaigns": top_campaigns,
    }
    return templates.TemplateResponse(request, "analytics.html", context)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    member_count_q = db.query(func.count(TenantMembership.id)).filter(TenantMembership.status == "active")
    if tenant_id is not None:
        member_count_q = member_count_q.filter(TenantMembership.tenant_id == tenant_id)
    member_count = member_count_q.scalar() or 0

    recent_auth = db.query(AuditLog).filter(AuditLog.entity_type == "auth")
    if tenant_id is not None:
        recent_auth = recent_auth.filter(AuditLog.tenant_id == tenant_id)
    recent_auth = recent_auth.order_by(AuditLog.created_at.desc()).limit(8).all()

    context = {
        **_base_context(request, "Settings"),
        "member_count": member_count,
        "recent_auth": recent_auth,
    }
    return templates.TemplateResponse(request, "settings.html", context)


@router.get("/feature-toggles", response_class=HTMLResponse)
def feature_toggles_page(request: Request) -> HTMLResponse:
    all_toggles = parse_feature_toggles(settings.feature_toggles_json)
    enabled_toggles = active_feature_toggles(settings.feature_toggles_json)

    context = {
        **_base_context(request, "Feature Toggles"),
        "enabled_toggles": dict(sorted(enabled_toggles.items())),
        "enabled_count": len(enabled_toggles),
        "total_count": len(all_toggles),
    }
    return templates.TemplateResponse(request, "feature_toggles.html", context)


def _is_feature_enabled(toggle_name: str) -> bool:
    toggles = parse_feature_toggles(settings.feature_toggles_json)
    return bool(toggles.get(toggle_name, False))


def _delete_companies_with_related_objects(db: Session, company_ids: list[int]) -> int:
    company_domain_rows = (
        db.query(Company.domain, Company.tenant_id)
        .filter(
            Company.id.in_(company_ids),
            Company.domain.isnot(None),
            Company.tenant_id.isnot(None),
        )
        .all()
    )

    campaign_ids = [
        row[0]
        for row in db.query(Campaign.id)
        .filter(Campaign.company_id.in_(company_ids))
        .all()
    ]

    message_ids: list[int] = []
    if campaign_ids:
        message_ids = [
            row[0]
            for row in db.query(Message.id)
            .filter(Message.campaign_id.in_(campaign_ids))
            .all()
        ]

    if message_ids:
        db.query(Reply).filter(Reply.message_id.in_(message_ids)).delete(synchronize_session=False)

    if campaign_ids:
        db.query(Handoff).filter(Handoff.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
        db.query(Message).filter(Message.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
        db.query(Schedule).filter(Schedule.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
        db.query(Campaign).filter(Campaign.id.in_(campaign_ids)).delete(synchronize_session=False)

    db.query(Handoff).filter(Handoff.company_id.in_(company_ids)).delete(synchronize_session=False)
    db.query(CompanyPage).filter(CompanyPage.company_id.in_(company_ids)).delete(synchronize_session=False)
    db.query(Contact).filter(Contact.company_id.in_(company_ids)).delete(synchronize_session=False)

    # Remove sender domains linked to deleted companies by tenant/domain.
    for domain, tenant_id in company_domain_rows:
        if not domain or tenant_id is None:
            continue
        db.query(SenderDomain).filter(
            SenderDomain.tenant_id == tenant_id,
            SenderDomain.domain == domain,
        ).delete(synchronize_session=False)

    return db.query(Company).filter(Company.id.in_(company_ids)).delete(synchronize_session=False)


def _delete_user_for_tenant_scope(db: Session, tenant_id: int, user_id: int) -> tuple[bool, int]:
    membership_count = (
        db.query(func.count(TenantMembership.id))
        .filter(TenantMembership.user_id == user_id)
        .scalar()
        or 0
    )

    db.query(UserSession).filter(
        UserSession.user_id == user_id,
        UserSession.tenant_id == tenant_id,
    ).delete(synchronize_session=False)

    deleted_memberships = db.query(TenantMembership).filter(
        TenantMembership.user_id == user_id,
        TenantMembership.tenant_id == tenant_id,
    ).delete(synchronize_session=False)

    user_deleted = False
    if membership_count <= deleted_memberships:
        db.query(UserSession).filter(UserSession.user_id == user_id).delete(synchronize_session=False)
        db.query(EmailToken).filter(EmailToken.user_id == user_id).delete(synchronize_session=False)
        db.query(TenantInvite).filter(TenantInvite.invited_by_user_id == user_id).delete(synchronize_session=False)
        db.query(TenantMembership).filter(TenantMembership.user_id == user_id).delete(synchronize_session=False)
        db.query(User).filter(User.id == user_id).delete(synchronize_session=False)
        user_deleted = True

    return user_deleted, deleted_memberships


@router.get("/dev-features", response_class=HTMLResponse)
def dev_features_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    delete_company_enabled = _is_feature_enabled("DELETE_COMPANY")
    delete_users_enabled = _is_feature_enabled("DELETE_USERS")
    companies_query = _company_query(db, tenant_id).order_by(Company.created_at.desc(), Company.id.desc())
    companies = companies_query.limit(200).all()
    all_users_query = _all_tenant_user_rows_query(db).order_by(
        TenantMembership.tenant_id.asc(),
        TenantMembership.created_at.desc(),
        TenantMembership.id.desc(),
    )
    user_rows = all_users_query.limit(500).all()
    context = {
        **_base_context(request, "Dev Features"),
        "delete_company_enabled": delete_company_enabled,
        "delete_users_enabled": delete_users_enabled,
        "delete_company_path": "/ui/dev-features/delete-company",
        "delete_user_path": "/ui/dev-features/delete-user",
        "companies": companies,
        "company_count": companies_query.count(),
        "users": user_rows,
        "user_count": all_users_query.count(),
    }

    logger.info(f"Rendering dev features page with {len(companies)} companies and {len(user_rows)} users across all tenants")
    return templates.TemplateResponse(request, "dev_features.html", context)


@router.post("/dev-features/delete-company-by-name")
def dev_features_delete_company_by_name(
    request: Request,
    company_name: str = Form(...),
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if tenant_id is None:
        return _flash_redirect("/ui/dev-features", error="Нет активного тенанта")
    if not _is_feature_enabled("DELETE_COMPANY"):
        return _flash_redirect("/ui/dev-features", error="DELETE_COMPANY feature disabled")

    clean_name = _trim(company_name)
    if not clean_name:
        return _flash_redirect("/ui/dev-features", error="Укажите имя компании")

    target_name = clean_name.lower()
    companies = (
        _company_query(db, tenant_id)
        .filter(func.lower(func.coalesce(Company.name, "")) == target_name)
        .all()
    )
    if not companies:
        return _flash_redirect("/ui/dev-features", error=f"Компании с именем '{clean_name}' не найдены")

    company_ids = [item.id for item in companies]

    try:
        deleted_companies = _delete_companies_with_related_objects(db, company_ids)
        db.add(
            AuditLog(
                tenant_id=tenant_id,
                action="dev_feature_company_deleted_by_name",
                entity_type="company",
                details={
                    "company_name": clean_name,
                    "deleted_companies": deleted_companies,
                    "company_ids": company_ids,
                },
                reason="DELETE_COMPANY feature action from dev features page",
            )
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return _flash_redirect("/ui/dev-features", error=f"Ошибка удаления: {exc}")

    return _flash_redirect("/ui/dev-features", message=f"Удалено компаний: {deleted_companies}")


@router.post("/dev-features/delete-company")
def dev_features_delete_company(
    request: Request,
    company_id: int = Form(...),
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if tenant_id is None:
        return _flash_redirect("/ui/dev-features", error="Нет активного тенанта")
    if not _is_feature_enabled("DELETE_COMPANY"):
        return _flash_redirect("/ui/dev-features", error="DELETE_COMPANY feature disabled")

    company = _company_by_id(db, company_id, tenant_id)
    if not company:
        return _flash_redirect("/ui/dev-features", error="Компания не найдена")

    company_name = company.name or company.domain
    try:
        deleted_companies = _delete_companies_with_related_objects(db, [company.id])
        db.add(
            AuditLog(
                tenant_id=tenant_id,
                action="dev_feature_company_deleted",
                entity_type="company",
                entity_id=company.id,
                details={
                    "company_id": company.id,
                    "company_name": company_name,
                    "deleted_companies": deleted_companies,
                },
                reason="DELETE_COMPANY feature action from dev features page",
            )
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return _flash_redirect("/ui/dev-features", error=f"Ошибка удаления: {exc}")

    return _flash_redirect("/ui/dev-features", message=f"Компания удалена: {company_name}")


@router.post("/dev-features/delete-user")
def dev_features_delete_user(
    request: Request,
    user_id: int = Form(...),
    membership_tenant_id: int | None = Form(default=None),
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if not _is_feature_enabled("DELETE_USERS"):
        return _flash_redirect("/ui/dev-features", error="DELETE_USERS feature disabled")

    # Use explicit membership_tenant_id from form when provided (cross-tenant deletion).
    effective_tenant_id = membership_tenant_id if membership_tenant_id is not None else tenant_id
    if effective_tenant_id is None:
        return _flash_redirect("/ui/dev-features", error="Нет активного тенанта")

    member_row = (
        _tenant_user_rows_query(db, effective_tenant_id)
        .filter(User.id == user_id)
        .first()
    )
    if not member_row:
        return _flash_redirect("/ui/dev-features", error="Пользователь не найден в указанном tenant")

    _, user = member_row
    display_name = (user.full_name or user.email or str(user.id)).strip()
    try:
        company_ids = [
            row[0]
            for row in db.query(Company.id)
            .filter(Company.tenant_id == effective_tenant_id)
            .all()
        ]
        deleted_companies = 0
        if company_ids:
            deleted_companies = _delete_companies_with_related_objects(db, company_ids)

        user_deleted, deleted_memberships = _delete_user_for_tenant_scope(db, effective_tenant_id, user.id)
        db.add(
            AuditLog(
                tenant_id=effective_tenant_id,
                action="dev_feature_user_deleted",
                entity_type="user",
                entity_id=user.id,
                details={
                    "user_id": user.id,
                    "user_email": user.email,
                    "deleted_memberships": deleted_memberships,
                    "user_deleted": user_deleted,
                    "deleted_companies": deleted_companies,
                    "company_ids": company_ids,
                },
                reason="DELETE_USERS feature action from dev features page",
            )
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return _flash_redirect("/ui/dev-features", error=f"Ошибка удаления: {exc}")

    if user_deleted:
        return _flash_redirect("/ui/dev-features", message=f"Пользователь удален: {display_name}. Компаний удалено: {deleted_companies}")
    return _flash_redirect("/ui/dev-features", message=f"Пользователь удален из tenant: {display_name}. Компаний удалено: {deleted_companies}")


# ---------------------------------------------------------------------------
# Domains (DNS wizard) — Phase B
# ---------------------------------------------------------------------------

@router.get("/domains", response_class=HTMLResponse)
def domains_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    domains: list[SenderDomain] = []
    from sqlalchemy.orm import joinedload as _jl
    if tenant_id is not None:
        domains = (
            db.query(SenderDomain)
            .options(
                _jl(SenderDomain.dns_records),
                _jl(SenderDomain.dkim_keys),
                _jl(SenderDomain.managed_selectors).joinedload(ManagedDkimSelector.dkim_key_pair),
            )
            .filter(SenderDomain.tenant_id == tenant_id)
            .order_by(SenderDomain.id)
            .all()
        )
    context = {
        **_base_context(request, "DNS Wizard"),
        "domains": domains,
        "config": settings,
    }
    return templates.TemplateResponse(request, "domains.html", context)


@router.post("/domains/add")
def domains_add(
    request: Request,
    domain: str = Form(...),
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if tenant_id is None:
        return _flash_redirect("/ui/domains", error="Нет активного тенанта")
    try:
        sd = create_sender_domain_profile(db=db, tenant_id=tenant_id, domain=normalize_domain(domain))
    except ValueError as exc:
        return _flash_redirect("/ui/domains", error=str(exc))
    db.add(AuditLog(
        tenant_id=tenant_id,
        action="domain_added",
        entity_type="sender_domain",
        entity_id=sd.id,
        details={"domain": sd.domain, "dkim_mode": sd.dkim_mode},
    ))
    db.commit()
    return _flash_redirect("/ui/domains", message=f"Домен {sd.domain} добавлен. Скопируйте записи ниже и нажмите Check DNS.")


@router.post("/domains/{domain_id}/verify")
def domains_check_dns_legacy(
    domain_id: int,
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if tenant_id is None:
        return _flash_redirect("/ui/domains", error="Нет активного тенанта")
    from sqlalchemy.orm import joinedload as _jl
    sd = db.query(SenderDomain).filter(
        SenderDomain.id == domain_id, SenderDomain.tenant_id == tenant_id
    ).options(
        _jl(SenderDomain.dns_records),
        _jl(SenderDomain.dkim_keys),
        _jl(SenderDomain.managed_selectors).joinedload(ManagedDkimSelector.dkim_key_pair),
    ).first()
    if not sd:
        return _flash_redirect("/ui/domains", error="Домен не найден")
    verify_sender_domain(sd)
    db.add(AuditLog(
        tenant_id=tenant_id,
        action="domain_dns_checked",
        entity_type="sender_domain",
        entity_id=sd.id,
        details={"domain": sd.domain, "spf": sd.spf_status, "dkim": sd.dkim_status, "dmarc": sd.dmarc_status, "overall": sd.status},
    ))
    db.commit()
    if sd.send_enabled:
        return _flash_redirect("/ui/domains", message=f"{sd.domain} verified. Sending is now enabled for {sd.send_from_email}.")
    return _flash_redirect("/ui/domains", error=f"{sd.domain}: DNS still incomplete. Check the per-record statuses below.")


@router.post("/domains/{domain_id}/delete")
def domains_delete(
    domain_id: int,
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if tenant_id is None:
        return _flash_redirect("/ui/domains", error="Нет активного тенанта")
    sd = db.query(SenderDomain).filter(
        SenderDomain.id == domain_id, SenderDomain.tenant_id == tenant_id
    ).first()
    if not sd:
        return _flash_redirect("/ui/domains", error="Домен не найден")
    domain_name = sd.domain
    db.add(AuditLog(
        tenant_id=tenant_id,
        action="domain_deleted",
        entity_type="sender_domain",
        entity_id=domain_id,
        details={"domain": domain_name},
    ))
    db.delete(sd)
    db.commit()
    return _flash_redirect("/ui/domains", message=f"Домен {domain_name} удалён")


@router.post("/domains/{domain_id}/check-dns")
def domains_check_dns_new(
    domain_id: int,
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return domains_check_dns_legacy(domain_id=domain_id, request=request, tenant_id=tenant_id, db=db)


@router.post("/domains/{domain_id}/regenerate-dkim")
def domains_regenerate_dkim(
    domain_id: int,
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
    _roles=Depends(require_roles("owner", "admin")),
) -> RedirectResponse:
    if tenant_id is None:
        return _flash_redirect("/ui/domains", error="Нет активного тенанта")
    from sqlalchemy.orm import joinedload as _jl
    sd = db.query(SenderDomain).filter(
        SenderDomain.id == domain_id, SenderDomain.tenant_id == tenant_id
    ).options(
        _jl(SenderDomain.dns_records),
        _jl(SenderDomain.dkim_keys),
        _jl(SenderDomain.managed_selectors).joinedload(ManagedDkimSelector.dkim_key_pair),
    ).first()
    if not sd:
        return _flash_redirect("/ui/domains", error="Домен не найден")
    rotate_dkim(sd, db)
    db.add(AuditLog(
        tenant_id=tenant_id,
        action="domain_dkim_rotated",
        entity_type="sender_domain",
        entity_id=sd.id,
        details={"domain": sd.domain, "dkim_mode": sd.dkim_mode},
    ))
    db.commit()
    message = (
        f"Managed DKIM rotated for {sd.domain}. DNS records on the customer side stay the same."
        if sd.dkim_mode == "cname"
        else f"DKIM regenerated for {sd.domain}. Update the TXT record and run Check DNS again."
    )
    return _flash_redirect("/ui/domains", message=message)


@router.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    sent_q = db.query(func.count(Message.id)).join(Campaign, Campaign.id == Message.campaign_id).join(Company, Company.id == Campaign.company_id)
    company_q = db.query(func.count(Company.id))
    if tenant_id is not None:
        sent_q = sent_q.filter(Company.tenant_id == tenant_id)
        company_q = company_q.filter(Company.tenant_id == tenant_id)

    usage_sent = sent_q.filter(Message.direction == MessageDirection.outbound, Message.sent_at.isnot(None)).scalar() or 0
    usage_companies = company_q.scalar() or 0

    context = {
        **_base_context(request, "Billing"),
        "usage_sent": usage_sent,
        "usage_companies": usage_companies,
    }
    return templates.TemplateResponse(request, "billing.html", context)


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
    queue: str | None = None,
    needs_human: str | None = None,
    company_q: str | None = None,
    country: str | None = None,
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = (
        db.query(Reply, Message, Campaign, Company, Contact)
        .join(Message, Message.id == Reply.message_id)
        .join(Campaign, Campaign.id == Message.campaign_id)
        .join(Company, Company.id == Campaign.company_id)
        .outerjoin(Contact, Contact.id == Campaign.contact_id)
    )

    if tenant_id is not None:
        query = query.filter(Company.tenant_id == tenant_id)

    queue_norm = (queue or "").strip().lower()
    if queue_norm in {"new", "positive", "neutral", "objection", "auto-reply", "unsubscribe", "spam-risk", "closed"}:
        label_expr = func.lower(func.coalesce(Reply.label, ""))
        if queue_norm == "new":
            query = query.filter(or_(Reply.label.is_(None), label_expr == "", label_expr == "new"))
        elif queue_norm == "positive":
            query = query.filter(or_(label_expr.like("%positive%"), label_expr.like("%interested%"), label_expr.like("%warm%"), label_expr.like("%meeting%")))
        elif queue_norm == "neutral":
            query = query.filter(label_expr.like("%neutral%"))
        elif queue_norm == "objection":
            query = query.filter(or_(label_expr.like("%objection%"), label_expr.like("%not_interested%"), label_expr.like("%rejection%")))
        elif queue_norm == "auto-reply":
            query = query.filter(or_(label_expr.like("%auto%"), label_expr.like("%ooo%")))
        elif queue_norm == "unsubscribe":
            query = query.filter(label_expr.like("%unsubscribe%"))
        elif queue_norm == "spam-risk":
            query = query.filter(or_(label_expr.like("%spam%"), label_expr.like("%abuse%"), label_expr.like("%complaint%")))
        elif queue_norm == "closed":
            query = query.filter(
                Campaign.has_reply.is_(True),
                Campaign.status.in_([CampaignStatus.replied, CampaignStatus.completed, CampaignStatus.stopped]),
            )

    needs_human_bool = _to_bool(needs_human)
    if needs_human_bool is True:
        query = query.filter(Reply.needs_human.is_(True))
    elif needs_human_bool is False:
        query = query.filter(or_(Reply.needs_human.is_(False), Reply.needs_human.is_(None)))

    if company_q:
        like = f"%{company_q.strip()}%"
        query = query.filter(or_(Company.domain.ilike(like), Company.name.ilike(like)))

    if country:
        query = query.filter(Company.country == country)

    rows = query.order_by(Reply.created_at.desc()).limit(300).all()

    countries_q = db.query(Company.country)
    if tenant_id is not None:
        countries_q = countries_q.filter(Company.tenant_id == tenant_id)
    countries = [row[0] for row in countries_q.filter(Company.country.isnot(None)).distinct().order_by(Company.country.asc()).all()]

    context = {
        **_base_context(request, "Ответы"),
        "rows": rows,
        "countries": countries,
        "filters": {
            "queue": queue_norm,
            "needs_human": needs_human or "",
            "company_q": company_q or "",
            "country": country or "",
        },
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
        q = (
            db.query(AuditLog)
            .filter(AuditLog.action.in_(actions))
        )
        if tenant_id is not None:
            q = q.filter(AuditLog.tenant_id == tenant_id)
        grouped[key] = q.order_by(AuditLog.created_at.desc()).limit(30).all()

    auth_actions = [
        "auth_login_blocked",
        "auth_login_failed",
        "auth_login_denied",
        "auth_login_success",
        "auth_logout",
        "auth_logout_all",
        "auth_email_verified",
        "auth_invite_accepted",
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
    if tenant_id is not None:
        auth_query = auth_query.filter(AuditLog.tenant_id == tenant_id)
    auth_rows = auth_query.order_by(AuditLog.created_at.desc()).limit(100).all()

    recent_tasks = db.query(Task).order_by(Task.created_at.desc()).limit(50).all()
    recent_audit = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(100).all()

    error_actions = {
        "reply_unmatched_manual_review",
        "campaign_stopped",
        "outreach_skipped",
        "auth_login_failed",
        "auth_login_blocked",
        "ui_auth_login_failed",
        "ui_auth_login_blocked",
    }
    errors_q = db.query(AuditLog).filter(AuditLog.action.in_(error_actions))
    if tenant_id is not None:
        errors_q = errors_q.filter(AuditLog.tenant_id == tenant_id)
    errors = errors_q.order_by(AuditLog.created_at.desc()).limit(50).all()

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
    _raise_team_ui_disabled()
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
    _raise_team_ui_disabled()
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
    _raise_team_ui_disabled()
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
    _raise_team_ui_disabled()
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
    _membership=Depends(require_roles("viewer", "operator", "manager", "admin", "owner")),
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
    _membership=Depends(require_roles("viewer", "operator", "manager", "admin", "owner")),
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
    _membership=Depends(require_roles("viewer", "operator", "manager", "admin", "owner")),
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
        tenant_id=membership.tenant_id,
        details={"email": normalized_email},
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
    password_confirm: str = Form(...),
    full_name: str = Form(default=""),
    tenant_name: str = Form(default=""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.api.routes.auth import register as api_register
    from app.schemas.auth import RegisterRequest

    normalized_email = email.strip().lower()
    if password != password_confirm:
        return _flash_redirect("/ui/register", error="Пароли не совпадают")

    resolved_tenant_name = (tenant_name or "").strip()
    if not resolved_tenant_name:
        local_part = normalized_email.split("@", 1)[0] if "@" in normalized_email else "workspace"
        resolved_tenant_name = f"{local_part}-workspace"

    payload = RegisterRequest(
        email=normalized_email,
        password=password,
        full_name=_trim(full_name),
        tenant_name=resolved_tenant_name,
    )
    try:
        session_data = api_register(payload=payload, request=request, db=db)
    except HTTPException as exc:
        return _flash_redirect("/ui/register", error=str(exc.detail))

    response = _flash_redirect("/ui/onboarding/start", message="Аккаунт создан. Завершите onboarding.")
    response.set_cookie(
        key=settings.auth_session_cookie_name,
        value=session_data.token,
        httponly=True,
        secure=settings.auth_session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/onboarding/start", response_class=HTMLResponse)
def onboarding_start_page(
    request: Request,
    step: int = Query(default=1),
    domain_id: int | None = Query(default=None),
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Single-screen wizard: load all onboarding data."""
    logger.debug(
        "Onboarding page requested: tenant_id=%s step=%s domain_id=%s",
        tenant_id,
        step,
        domain_id,
    )
    if tenant_id is None or tenant_id <= 0:
        logger.error("Onboarding page failed: invalid tenant_id=%s", tenant_id)
        return _flash_redirect("/ui/login", error="Сессия не найдена")

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        logger.error("Onboarding page failed: tenant not found tenant_id=%s", tenant_id)
        return _flash_redirect("/ui/login", error="Workspace не найден")

    onboarding_completed = (
        db.query(AuditLog.id)
        .filter(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == "onboarding_completed",
            AuditLog.entity_type == "tenant",
            AuditLog.entity_id == tenant_id,
        )
        .first()
        is not None
    )
    if onboarding_completed:
        logger.info("Onboarding page skipped: already completed tenant_id=%s", tenant_id)
        return RedirectResponse(url="/ui", status_code=303)

    company = db.query(Company).filter(Company.tenant_id == tenant_id).order_by(Company.id.asc()).first()
    from sqlalchemy.orm import joinedload as _jl
    sender_domains = (
        db.query(SenderDomain)
        .options(_jl(SenderDomain.dns_records))
        .filter(SenderDomain.tenant_id == tenant_id)
        .order_by(SenderDomain.id.asc())
        .all()
    )
    purpose_order = {"spf": 0, "dkim": 1, "dmarc": 2}
    for item in sender_domains:
        item.dns_records = sorted(
            item.dns_records,
            key=lambda rec: (purpose_order.get((rec.purpose or "").lower(), 99), rec.id),
        )
    ownership_changed = False
    for item in sender_domains:
        before_token = item.ownership_token
        ensure_ownership_token(item)
        if item.ownership_token != before_token:
            ownership_changed = True
    if ownership_changed:
        db.commit()
        logger.info(
            "Onboarding page: ownership tokens updated tenant_id=%s domains_updated=%s",
            tenant_id,
            len(sender_domains),
        )
    sender_domain = sender_domains[0] if sender_domains else None

    last_company_audit = (
        db.query(AuditLog)
        .filter(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == "onboarding_company_saved",
            AuditLog.entity_type == "tenant",
            AuditLog.entity_id == tenant_id,
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    sender_name_value = ""
    if last_company_audit and isinstance(last_company_audit.details, dict):
        sender_name_value = str(last_company_audit.details.get("sender_name") or "").strip()

    initial_step = max(1, min(step, 3))
    step2_completed = bool(
        sender_domain
        and sender_domain.ownership_status == "verified"
        and authentication_status(sender_domain) == "authenticated"
    )
    logger.debug(
        "Onboarding page state: tenant_id=%s company_exists=%s sender_domains=%s initial_step=%s step2_completed=%s",
        tenant_id,
        company is not None,
        len(sender_domains),
        initial_step,
        step2_completed,
    )

    context = {
        **_base_context(request, "Let's set up your workspace"),
        "workspace_name": tenant.name or "",
        "company_name": company.name if company else "",
        "company_website": f"https://{company.domain}" if company and company.domain else "",
        "company_domain": company.domain if company else "",
        "domain_id": sender_domain.id if sender_domain else None,
        "domain_status": sender_domain.status if sender_domain else "pending",
        "domain_ownership_status": sender_domain.ownership_status if sender_domain else "pending",
        "domain_verified": sender_domain.send_enabled if sender_domain else False,
        "domain_spf_status": sender_domain.spf_status if sender_domain else "pending",
        "domain_dkim_status": sender_domain.dkim_status if sender_domain else "pending",
        "domain_dmarc_status": sender_domain.dmarc_status if sender_domain else "pending",
        "domain_dns_records": sender_domain.dns_records if sender_domain else [],
        "sender_domains": sender_domains,
        "step2_completed": step2_completed,
        "sender_name": sender_name_value,
        "initial_step": initial_step,
        "initial_domain_id": domain_id,
    }
    logger.info(
        "Onboarding page ready: tenant_id=%s initial_step=%s selected_domain_id=%s",
        tenant_id,
        initial_step,
        sender_domain.id if sender_domain else None,
    )
    return templates.TemplateResponse(request, "onboarding_start.html", context)


@router.post("/onboarding/start", response_model=None)
def onboarding_start_submit(
    request: Request,
    step: int = Form(...),
    action: str = Form(default="next"),
    workspace_name: str = Form(default=""),
    company_website: str = Form(default=""),
    company_name: str = Form(default=""),
    sender_name: str = Form(default=""),
    domain_id: int | None = Form(default=None),
    verify_domain: str = Form(default=""),
    ownership_email: str = Form(default=""),
    tenant_id: int | None = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> RedirectResponse | dict:
    """Handle all onboarding steps (1-3) within single screen."""
    logger.debug(
        "Onboarding submit received: tenant_id=%s step=%s action=%s domain_id=%s",
        tenant_id,
        step,
        action,
        domain_id,
    )
    if tenant_id is None or tenant_id <= 0:
        logger.error("Onboarding submit failed: invalid tenant_id=%s", tenant_id)
        return {"success": False, "error": "Сессия не найдена"}

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        logger.error("Onboarding submit failed: tenant not found for tenant_id=%s", tenant_id)
        return {"success": False, "error": "Workspace не найден"}

    onboarding_completed = (
        db.query(AuditLog.id)
        .filter(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == "onboarding_completed",
            AuditLog.entity_type == "tenant",
            AuditLog.entity_id == tenant_id,
        )
        .first()
        is not None
    )
    if onboarding_completed:
        logger.info("Onboarding submit skipped: onboarding already completed for tenant_id=%s", tenant_id)
        return RedirectResponse(url="/ui", status_code=303)

    # Step 1: Company details
    if step == 1:
        logger.debug("Onboarding step=1 started for tenant_id=%s", tenant_id)
        if not company_website.strip():
            logger.error("Onboarding step=1 failed: company_website is empty tenant_id=%s", tenant_id)
            return {"success": False, "error": "Укажите website компании"}

        domain = _extract_domain_from_website(company_website)
        if domain is None:
            logger.error("Onboarding step=1 failed: invalid company_website format tenant_id=%s", tenant_id)
            return {"success": False, "error": "Некорректный формат website"}

        duplicate = db.query(Company).filter(Company.domain == domain, Company.tenant_id != tenant_id).first()
        if duplicate is not None:
            logger.error(
                "Onboarding step=1 failed: domain already used in another workspace tenant_id=%s domain=%s",
                tenant_id,
                domain,
            )
            return {"success": False, "error": "Этот домен уже используется в другом workspace"}

        company = db.query(Company).filter(Company.domain == domain, Company.tenant_id == tenant_id).first()
        if company is None:
            company = db.query(Company).filter(Company.tenant_id == tenant_id).order_by(Company.id.asc()).first()

        if company is None:
            company = Company(
                tenant_id=tenant_id,
                domain=domain,
                name=_trim(company_name) or domain.split('.')[0].title(),
                status=CompanyStatus.new,
            )
            db.add(company)
        else:
            company.domain = domain
            company.name = _trim(company_name) or company.name or domain.split('.')[0].title()

        # Generate workspace_name if not provided (should be auto-generated on frontend)
        if not workspace_name.strip():
            workspace_name = _trim(company_name) or domain.split('.')[0].title()

        sender_domain = db.query(SenderDomain).filter(SenderDomain.tenant_id == tenant_id).order_by(SenderDomain.id.asc()).first()
        if sender_domain is not None and sender_domain.domain != domain:
            logger.error(
                "Onboarding step=1 failed: sender domain mismatch tenant_id=%s existing_domain=%s requested_domain=%s",
                tenant_id,
                sender_domain.domain,
                domain,
            )
            return {
                "success": False,
                "error": (
                    "Для workspace уже настроен другой sender domain. "
                    "Измените его в DNS Wizard (/ui/domains)."
                ),
            }

        if sender_domain is None:
            try:
                sender_domain = create_sender_domain_profile(db=db, tenant_id=tenant_id, domain=domain)
                logger.info(
                    "Onboarding step=1: sender domain profile auto-created tenant_id=%s sender_domain_id=%s domain=%s",
                    tenant_id,
                    sender_domain.id,
                    sender_domain.domain,
                )
            except ValueError as exc:
                logger.error(
                    "Onboarding step=1 failed: auto-create sender domain error tenant_id=%s domain=%s error=%s",
                    tenant_id,
                    domain,
                    str(exc),
                )
                return {"success": False, "error": str(exc)}
        else:
            ensure_ownership_token(sender_domain)

        tenant.name = workspace_name.strip() or tenant.name

        db.add(AuditLog(
            tenant_id=tenant_id,
            action="onboarding_company_saved",
            entity_type="tenant",
            entity_id=tenant_id,
            details={
                "workspace_name": tenant.name,
                "website": company_website.strip(),
                "domain": domain,
                "sender_name": _trim(sender_name) or "",
            },
        ))
        db.commit()
        logger.info("Onboarding step=1 completed: company saved tenant_id=%s domain=%s", tenant_id, domain)

        return {"success": True, "next_step": 2, "reload": True}

    # Step 2: Sender domain management with explicit actions.
    if step == 2:
        logger.debug("Onboarding step=2 started: tenant_id=%s action=%s domain_id=%s", tenant_id, action, domain_id)
        from sqlalchemy.orm import joinedload as _jl
        sender_domain = None
        if domain_id is not None:
            sender_domain = (
                db.query(SenderDomain)
                .options(_jl(SenderDomain.dns_records))
                .filter(SenderDomain.id == domain_id, SenderDomain.tenant_id == tenant_id)
                .first()
            )
        if sender_domain is None:
            sender_domain = (
                db.query(SenderDomain)
                .options(_jl(SenderDomain.dns_records))
                .filter(SenderDomain.tenant_id == tenant_id)
                .order_by(SenderDomain.id.asc())
                .first()
            )

        if action == "add_domain":
            logger.debug("Onboarding step=2 action=add_domain tenant_id=%s", tenant_id)
            actor = _resolve_request_user(request=request, db=db, tenant_id=tenant_id)
            if actor is None:
                logger.error("Onboarding step=2 add_domain failed: actor not resolved tenant_id=%s", tenant_id)
                return {"success": False, "error": "Please sign in again before adding a sending domain."}
            if not actor.email_verified:
                logger.error("Onboarding step=2 add_domain failed: actor email not verified user_id=%s", actor.id)
                return {"success": False, "error": "Please verify your email before adding a sending domain."}
            if not actor.is_active:
                logger.error("Onboarding step=2 add_domain failed: actor inactive user_id=%s", actor.id)
                return {"success": False, "error": "Your account must be activated before adding a sending domain."}

            company = db.query(Company).filter(Company.tenant_id == tenant_id).order_by(Company.id.asc()).first()
            if company is None or not company.domain:
                logger.error("Onboarding step=2 add_domain failed: company/domain missing tenant_id=%s", tenant_id)
                return {"success": False, "error": "Add your company website on step 1 before adding a sender domain."}

            if sender_domain is None:
                try:
                    sender_domain = create_sender_domain_profile(db=db, tenant_id=tenant_id, domain=company.domain)
                except ValueError as exc:
                    logger.error(
                        "Onboarding step=2 add_domain failed: create profile error tenant_id=%s domain=%s error=%s",
                        tenant_id,
                        company.domain,
                        str(exc),
                    )
                    return {"success": False, "error": str(exc)}
                db.add(AuditLog(
                    tenant_id=tenant_id,
                    action="onboarding_domain_records_generated",
                    entity_type="sender_domain",
                    entity_id=sender_domain.id,
                    details={"domain": sender_domain.domain},
                ))
                db.commit()
                logger.info(
                    "Onboarding step=2 add_domain completed: profile created tenant_id=%s sender_domain_id=%s domain=%s",
                    tenant_id,
                    sender_domain.id,
                    sender_domain.domain,
                )
            else:
                ensure_ownership_token(sender_domain)
                db.commit()
                logger.info(
                    "Onboarding step=2 add_domain completed: existing domain token ensured tenant_id=%s sender_domain_id=%s",
                    tenant_id,
                    sender_domain.id,
                )
            return {
                "success": True,
                "next_step": 2,
                "reload": True,
                "open_domain_id": sender_domain.id,
            }

        if action == "verify_ownership_email":
            logger.debug("Onboarding step=2 action=verify_ownership_email tenant_id=%s", tenant_id)
            if sender_domain is None:
                logger.error("Onboarding step=2 verify_ownership_email failed: sender_domain missing tenant_id=%s", tenant_id)
                return {"success": False, "error": "Add a sender domain first."}

            actor = _resolve_request_user(request=request, db=db, tenant_id=tenant_id)
            if actor is None:
                logger.error("Onboarding step=2 verify_ownership_email failed: actor not resolved tenant_id=%s", tenant_id)
                return {"success": False, "error": "Please sign in again before requesting email verification."}

            submitted_email = ownership_email.strip()
            email_domain = _extract_email_domain(submitted_email)
            if email_domain is None:
                logger.error("Onboarding step=2 verify_ownership_email failed: invalid email tenant_id=%s", tenant_id)
                return {"success": False, "error": "Please enter a valid email address."}

            expected_domain = (sender_domain.domain or "").strip().lower()
            if email_domain != expected_domain:
                logger.error(
                    "Onboarding step=2 verify_ownership_email failed: email domain mismatch tenant_id=%s expected=%s actual=%s",
                    tenant_id,
                    expected_domain,
                    email_domain,
                )
                return {
                    "success": False,
                    "error": f"This email must belong to the domain being verified: {sender_domain.domain}",
                }

            token_purpose = _domain_ownership_email_token_purpose(sender_domain.id)
            raw_token = create_email_token(db, user_id=actor.id, purpose=token_purpose)
            verify_query = urlencode({
                "token": raw_token,
                "domain_id": sender_domain.id,
                "email": submitted_email,
            })
            verify_url = f"{settings.app_public_base_url}/ui/verify-domain-ownership?{verify_query}"

            try:
                message_id = send_domain_ownership_verification_email(
                    to_email=submitted_email,
                    domain=sender_domain.domain,
                    workspace_name=tenant.name or "workspace",
                    verification_url=verify_url,
                )
            except Exception:
                logger.error(
                    "Onboarding step=2 verify_ownership_email failed: mail send error tenant_id=%s sender_domain_id=%s",
                    tenant_id,
                    sender_domain.id,
                    exc_info=True,
                )
                return {
                    "success": False,
                    "error": "Failed to send verification email. Please check mail settings and try again.",
                }

            mark_sender_domain_ownership_email_pending(sender_domain, submitted_email)
            db.add(AuditLog(
                tenant_id=tenant_id,
                action="onboarding_domain_ownership_email_requested",
                entity_type="sender_domain",
                entity_id=sender_domain.id,
                details={
                    "domain": sender_domain.domain,
                    "ownership_status": sender_domain.ownership_status,
                    "ownership_verified_via": sender_domain.ownership_verified_via,
                    "ownership_email": sender_domain.ownership_email,
                    "message_id": message_id,
                    "verify_url": verify_url,
                },
            ))
            db.commit()
            logger.info(
                "Onboarding step=2 verify_ownership_email completed: verification link sent tenant_id=%s sender_domain_id=%s ownership_email=%s",
                tenant_id,
                sender_domain.id,
                sender_domain.ownership_email,
            )
            return {
                "success": True,
                "next_step": 2,
                "reload": True,
                "open_domain_id": sender_domain.id,
            }

        if action == "verify_ownership_dns":
            logger.debug("Onboarding step=2 action=verify_ownership_dns tenant_id=%s", tenant_id)
            if sender_domain is None:
                logger.error("Onboarding step=2 verify_ownership_dns failed: sender_domain missing tenant_id=%s", tenant_id)
                return {"success": False, "error": "Add a sender domain first."}

            ensure_ownership_token(sender_domain)
            ownership_result = verify_sender_domain_ownership_dns(sender_domain)
            db.add(AuditLog(
                tenant_id=tenant_id,
                action="onboarding_domain_ownership_dns_checked",
                entity_type="sender_domain",
                entity_id=sender_domain.id,
                details={
                    "domain": sender_domain.domain,
                    "ownership_status": sender_domain.ownership_status,
                    "ownership_method": sender_domain.ownership_method,
                    "ownership_verified_via": sender_domain.ownership_verified_via,
                    "ownership_host": sender_domain.ownership_host,
                    "ownership_result": ownership_result.status,
                },
            ))
            db.commit()
            logger.info(
                "Onboarding step=2 verify_ownership_dns result: tenant_id=%s sender_domain_id=%s result=%s",
                tenant_id,
                sender_domain.id,
                ownership_result.status,
            )

            if ownership_result.status != "verified":
                if ownership_result.status == "missing":
                    logger.error(
                        "Onboarding step=2 verify_ownership_dns failed: ownership TXT missing tenant_id=%s sender_domain_id=%s",
                        tenant_id,
                        sender_domain.id,
                    )
                    return {
                        "success": False,
                        "error": "Ownership TXT record is not found yet. Please add the record and wait for DNS propagation.",
                    }
                logger.error(
                    "Onboarding step=2 verify_ownership_dns failed: ownership TXT mismatch tenant_id=%s sender_domain_id=%s",
                    tenant_id,
                    sender_domain.id,
                )
                return {
                    "success": False,
                    "error": "Ownership TXT record exists but does not match the expected verification token.",
                }

            return {
                "success": True,
                "next_step": 2,
                "reload": True,
                "open_domain_id": sender_domain.id,
            }

        if action == "check_dns":
            logger.debug("Onboarding step=2 action=check_dns tenant_id=%s", tenant_id)
            if sender_domain is None:
                logger.error("Onboarding step=2 check_dns failed: sender_domain missing tenant_id=%s", tenant_id)
                return {"success": False, "error": "Add a sender domain first."}

            verify_sender_domain(sender_domain)
            db.add(AuditLog(
                tenant_id=tenant_id,
                action="onboarding_domain_dns_checked",
                entity_type="sender_domain",
                entity_id=sender_domain.id,
                details={
                    "domain": sender_domain.domain,
                    "spf": sender_domain.spf_status,
                    "dkim": sender_domain.dkim_status,
                    "dmarc": sender_domain.dmarc_status,
                    "authentication": authentication_status(sender_domain),
                    "token": verify_domain.strip() if verify_domain.strip() else None,
                },
            ))
            db.commit()
            logger.info(
                "Onboarding step=2 check_dns completed: tenant_id=%s sender_domain_id=%s authentication=%s",
                tenant_id,
                sender_domain.id,
                authentication_status(sender_domain),
            )
            return {
                "success": True,
                "next_step": 2,
                "reload": True,
                "open_domain_id": sender_domain.id,
            }

        if action == "next":
            logger.debug("Onboarding step=2 action=next tenant_id=%s", tenant_id)
            if sender_domain is None:
                logger.error("Onboarding step=2 next failed: sender_domain missing tenant_id=%s", tenant_id)
                return {"success": False, "error": "Add and verify a sender domain before continuing."}
            step2_completed = (
                sender_domain.ownership_status == "verified"
                and authentication_status(sender_domain) == "authenticated"
            )
            if not step2_completed:
                logger.error(
                    "Onboarding step=2 next failed: step2 not completed tenant_id=%s sender_domain_id=%s ownership=%s auth=%s",
                    tenant_id,
                    sender_domain.id,
                    sender_domain.ownership_status,
                    authentication_status(sender_domain),
                )
                return {
                    "success": False,
                    "error": "Domain setup is not complete yet. Verify ownership and run authentication DNS checks.",
                }
            sender_email_clean = sender_domain.send_from_email.strip().lower()
            configured_sender_name = _trim(sender_name) or ""
            if not configured_sender_name:
                last_company_audit = (
                    db.query(AuditLog)
                    .filter(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action == "onboarding_company_saved",
                        AuditLog.entity_type == "tenant",
                        AuditLog.entity_id == tenant_id,
                    )
                    .order_by(AuditLog.id.desc())
                    .first()
                )
                if last_company_audit and isinstance(last_company_audit.details, dict):
                    configured_sender_name = str(last_company_audit.details.get("sender_name") or "").strip()
            db.add(AuditLog(
                tenant_id=tenant_id,
                action="onboarding_sender_configured",
                entity_type="tenant",
                entity_id=tenant_id,
                details={
                    "sender_name": configured_sender_name,
                    "sender_email": sender_email_clean,
                    "source": "auto_from_verified_domain",
                },
            ))
            db.commit()
            logger.info(
                "Onboarding step=2 completed: sender configured tenant_id=%s sender_email=%s",
                tenant_id,
                sender_email_clean,
            )
            return {"success": True, "next_step": 3}

        logger.error("Onboarding step=2 failed: unknown action tenant_id=%s action=%s", tenant_id, action)
        return {"success": False, "error": "Unknown step 2 action."}

    # Step 3: Complete onboarding
    if step == 3:
        logger.debug("Onboarding step=3 started: tenant_id=%s action=%s", tenant_id, action)
        if action == "finish":
            db.add(AuditLog(
                tenant_id=tenant_id,
                action="onboarding_completed",
                entity_type="tenant",
                entity_id=tenant_id,
                details={"completed_at": datetime.utcnow().isoformat()},
                reason="User completed onboarding wizard.",
            ))
            db.commit()
            logger.info("Onboarding step=3 completed: onboarding finished tenant_id=%s", tenant_id)
            return RedirectResponse(url="/ui", status_code=303)
        logger.debug("Onboarding step=3 no-op action tenant_id=%s action=%s", tenant_id, action)
        return {"success": True}

    logger.error("Onboarding submit failed: invalid step tenant_id=%s step=%s", tenant_id, step)
    return {"success": False, "error": "Invalid step"}


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


@router.get("/verify-domain-ownership")
def verify_domain_ownership_page(
    request: Request,
    token: str | None = None,
    domain_id: int | None = None,
    email: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if not token or domain_id is None:
        return _flash_redirect("/ui/onboarding/start?step=2", error="Verification link is invalid.")

    row = consume_email_token(db, token, _domain_ownership_email_token_purpose(domain_id))
    if row is None:
        return _flash_redirect(
            f"/ui/onboarding/start?step=2&domain_id={domain_id}",
            error="Verification link is invalid or expired.",
        )

    sender_domain = db.query(SenderDomain).filter(SenderDomain.id == domain_id).first()
    if sender_domain is None:
        return _flash_redirect("/ui/onboarding/start?step=2", error="Sender domain not found.")

    membership = (
        db.query(TenantMembership)
        .filter(
            TenantMembership.user_id == row.user_id,
            TenantMembership.tenant_id == sender_domain.tenant_id,
        )
        .first()
    )
    if membership is None:
        return _flash_redirect("/ui/login", error="Verification link is invalid for this workspace.")

    effective_email = (sender_domain.ownership_email or "").strip()
    if not effective_email:
        effective_email = (email or "").strip()
    if not effective_email:
        return _flash_redirect(
            f"/ui/onboarding/start?step=2&domain_id={sender_domain.id}",
            error="Verification email is missing.",
        )

    mark_sender_domain_ownership_email_verified(sender_domain, effective_email)
    db.add(AuditLog(
        tenant_id=sender_domain.tenant_id,
        action="onboarding_domain_ownership_email_verified",
        entity_type="sender_domain",
        entity_id=sender_domain.id,
        details={
            "domain": sender_domain.domain,
            "ownership_status": sender_domain.ownership_status,
            "ownership_verified_via": sender_domain.ownership_verified_via,
            "ownership_email": sender_domain.ownership_email,
            "verified_by_user_id": row.user_id,
        },
    ))
    db.commit()
    return _flash_redirect(
        f"/ui/onboarding/start?step=2&domain_id={sender_domain.id}",
        message="Domain ownership verified by email.",
    )


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

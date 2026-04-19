from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.db.session import SessionLocal, engine
from app.services.auth.session_manager import resolve_active_session, rotate_session_if_needed
from app.ui.csrf import is_valid_csrf, issue_csrf_cookie_if_missing, should_enforce_csrf
from app.ui import router as ui_router
import app.models  # noqa: F401 - registers all models with metadata

app = FastAPI(title=settings.app_name, debug=settings.app_debug)

_APP_DIR = Path(__file__).resolve().parent
_UI_STATIC_DIR = _APP_DIR / "ui" / "static"

_UI_PUBLIC_PATH_PREFIXES = (
    "/ui/login",
    "/ui/register",
    "/ui/resend-verification",
    "/ui/forgot-password",
    "/ui/reset-password",
    "/ui/accept-invite",
    "/ui/verify-email",
    "/ui/verify-domain-ownership",
)

_UI_ONBOARDING_ALLOWED_PATH_PREFIXES = (
    "/ui/onboarding",
    "/ui/domains",
    "/ui/logout",
)


def _extract_bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "bearer "
    raw = value.strip()
    if raw.lower().startswith(prefix):
        token = raw[len(prefix):].strip()
        return token or None
    return None


def _is_ui_public_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _UI_PUBLIC_PATH_PREFIXES)


def _is_ui_onboarding_allowed_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _UI_ONBOARDING_ALLOWED_PATH_PREFIXES)


@app.on_event("startup")
def on_startup() -> None:
    if settings.app_auto_create_tables:
        Base.metadata.create_all(bind=engine)


@app.middleware("http")
async def ui_csrf_middleware(request, call_next):
    path = request.url.path
    # Protect only real UI pages, not /ui-static assets.
    is_ui = path == "/ui" or path.startswith("/ui/")
    onboarding_required = False

    if is_ui and not _is_ui_public_path(path):
        raw_token = _extract_bearer_token(request.headers.get("Authorization"))
        if raw_token is None:
            raw_token = request.cookies.get(settings.auth_session_cookie_name)

        active_session = None
        if raw_token:
            header_token = _extract_bearer_token(request.headers.get("Authorization"))
            override = request.app.dependency_overrides.get(get_db)
            override_gen = None
            db = None
            if override is not None:
                override_gen = override()
                try:
                    db = next(override_gen)
                except StopIteration:
                    db = None
            if db is None:
                db = SessionLocal()
            try:
                active_session = resolve_active_session(db, raw_token)
                if active_session is not None:
                    from app.models.company import Company
                    from app.models.sender_domain import SenderDomain

                    effective_tenant_id = active_session.tenant_id
                    if header_token is not None:
                        header_tenant_id_raw = request.headers.get("X-Tenant-Id")
                        if header_tenant_id_raw:
                            try:
                                effective_tenant_id = int(header_tenant_id_raw)
                            except ValueError:
                                pass

                    has_verified_domain = (
                        db.query(SenderDomain.id)
                        .filter(
                            SenderDomain.tenant_id == effective_tenant_id,
                            SenderDomain.send_enabled.is_(True),
                        )
                        .first()
                        is not None
                    )
                    from app.models.audit_log import AuditLog

                    onboarding_completed = (
                        db.query(AuditLog.id)
                        .filter(
                            AuditLog.tenant_id == effective_tenant_id,
                            AuditLog.action == "onboarding_completed",
                            AuditLog.entity_type == "tenant",
                            AuditLog.entity_id == effective_tenant_id,
                        )
                        .first()
                        is not None
                    )
                    has_any_company = (
                        db.query(Company.id)
                        .filter(Company.tenant_id == effective_tenant_id)
                        .first()
                        is not None
                    )
                    onboarding_required = (
                        (not onboarding_completed)
                        and (not has_verified_domain)
                        and (not has_any_company)
                    )
            finally:
                if override_gen is not None:
                    try:
                        next(override_gen)
                    except StopIteration:
                        pass
                else:
                    db.close()

        if active_session is None:
            params = {}
            msg = request.query_params.get("msg")
            err = request.query_params.get("err")
            if msg:
                params["msg"] = msg
            if err:
                params["err"] = err
            login_url = "/ui/login"
            if params:
                login_url = f"{login_url}?{urlencode(params)}"
            return RedirectResponse(url=login_url, status_code=303)

        if onboarding_required and not _is_ui_onboarding_allowed_path(path):
            return RedirectResponse(url="/ui/onboarding/start", status_code=303)

    is_unsafe = request.method in {"POST", "PUT", "PATCH", "DELETE"}
    if is_ui and is_unsafe and should_enforce_csrf(request):
        if not await is_valid_csrf(request):
            return PlainTextResponse("csrf_invalid", status_code=403)

    response = await call_next(request)
    if is_ui:
        issue_csrf_cookie_if_missing(request, response)

        # Rotate auth cookie session close to expiry to keep browser sessions seamless.
        raw_token = request.cookies.get(settings.auth_session_cookie_name)
        if raw_token:
            db = SessionLocal()
            try:
                try:
                    session = resolve_active_session(db, raw_token)
                    if session is not None:
                        rotated = rotate_session_if_needed(
                            db,
                            session,
                            rotate_before_hours=settings.auth_session_rotate_before_hours,
                        )
                        if rotated:
                            response.set_cookie(
                                key=settings.auth_session_cookie_name,
                                value=rotated,
                                httponly=True,
                                secure=settings.auth_session_cookie_secure,
                                samesite="lax",
                                path="/",
                            )
                except Exception:
                    # Rotation is best-effort and must not fail regular UI requests.
                    pass
            finally:
                db.close()
    return response


app.include_router(api_router, prefix="/api")
app.include_router(ui_router)
app.mount("/ui-static", StaticFiles(directory=str(_UI_STATIC_DIR)), name="ui_static")

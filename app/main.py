from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import settings
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.services.auth.session_manager import resolve_active_session, rotate_session_if_needed
from app.ui.csrf import is_valid_csrf, issue_csrf_cookie_if_missing, should_enforce_csrf
from app.ui import router as ui_router
import app.models  # noqa: F401 - registers all models with metadata

app = FastAPI(title=settings.app_name, debug=settings.app_debug)

_APP_DIR = Path(__file__).resolve().parent
_UI_STATIC_DIR = _APP_DIR / "ui" / "static"


@app.on_event("startup")
def on_startup() -> None:
    if settings.app_auto_create_tables:
        Base.metadata.create_all(bind=engine)


@app.middleware("http")
async def ui_csrf_middleware(request, call_next):
    is_ui = request.url.path.startswith("/ui")
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

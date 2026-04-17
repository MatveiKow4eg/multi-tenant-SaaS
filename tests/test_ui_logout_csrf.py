from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app


def _build_test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _cookie_value_from_set_cookie(set_cookie_header: str, cookie_name: str) -> str:
    parts = set_cookie_header.split(";")
    for part in parts:
        item = part.strip()
        prefix = f"{cookie_name}="
        if item.startswith(prefix):
            return item[len(prefix):]
    return ""


def test_ui_post_requires_csrf_when_auth_cookie_present(monkeypatch):
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    def _fake_send_invite_email(*, to_email: str, invite_url: str, tenant_name: str, role: str):
        return "msg-1"

    class _FakeConnectivity:
        smtp_ok = True
        imap_ok = True

    monkeypatch.setattr("app.ui.routes.send_invite_email", _fake_send_invite_email)
    monkeypatch.setattr("app.ui.routes.check_zone_connectivity", lambda: _FakeConnectivity())
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        reg_owner = client.post(
            "/api/auth/register",
            json={
                "email": "owner@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Alpha Team",
            },
        )
        assert reg_owner.status_code == 200

        auth_token = reg_owner.json()["token"]
        client.cookies.set("auth_session", auth_token)

        # Issue CSRF cookie via UI GET.
        page = client.get("/ui/team")
        assert page.status_code == 200
        csrf_token = client.cookies.get("csrf_token")
        assert csrf_token

        denied = client.post(
            "/ui/team/invite",
            data={
                "email": "invitee@example.com",
                "role": "operator",
                "expires_in_hours": "24",
            },
            follow_redirects=False,
        )
        assert denied.status_code == 403

        allowed = client.post(
            "/ui/team/invite",
            data={
                "email": "invitee@example.com",
                "role": "operator",
                "expires_in_hours": "24",
                "_csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert allowed.status_code == 303
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_ui_logout_clears_cookie_and_invalidates_session():
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        reg_owner = client.post(
            "/api/auth/register",
            json={
                "email": "owner@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Alpha Team",
            },
        )
        assert reg_owner.status_code == 200
        auth_token = reg_owner.json()["token"]
        client.cookies.set("auth_session", auth_token)

        # Load any UI page to obtain csrf cookie.
        page = client.get("/ui")
        assert page.status_code == 200
        csrf_token = client.cookies.get("csrf_token")
        assert csrf_token

        logout = client.post(
            "/ui/logout",
            data={"_csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert logout.status_code == 303

        me = client.get("/api/auth/me")
        assert me.status_code == 401
    finally:
        app.dependency_overrides.clear()
        db.close()

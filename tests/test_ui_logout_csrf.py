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


def test_ui_team_routes_are_disabled(monkeypatch):
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    class _FakeConnectivity:
        smtp_ok = True
        imap_ok = True

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

        page = client.get("/ui/team")
        assert page.status_code == 404

        ui_home = client.get("/ui")
        assert ui_home.status_code == 200
        csrf_token = client.cookies.get("csrf_token")
        assert csrf_token

        invite_resp = client.post(
            "/ui/team/invite",
            data={
                "email": "invitee@example.com",
                "role": "operator",
                "expires_in_hours": "24",
            },
            follow_redirects=False,
        )
        assert invite_resp.status_code == 403

        invite_resp_with_csrf = client.post(
            "/ui/team/invite",
            data={
                "email": "invitee@example.com",
                "role": "operator",
                "expires_in_hours": "24",
                "_csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert invite_resp_with_csrf.status_code == 404
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
        assert logout.headers["location"].startswith("/ui?msg=")

        private_after_logout = client.get(logout.headers["location"], follow_redirects=False)
        assert private_after_logout.status_code == 303
        assert private_after_logout.headers["location"].startswith("/ui/login")

        login_page = client.get(private_after_logout.headers["location"])
        assert login_page.status_code == 200
        assert "Dashboard" not in login_page.text
        assert "Companies" not in login_page.text
        assert "Вы вышли из системы" in login_page.text

        me = client.get("/api/auth/me")
        assert me.status_code == 401
    finally:
        app.dependency_overrides.clear()
        db.close()

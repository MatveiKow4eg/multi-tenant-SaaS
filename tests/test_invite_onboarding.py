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


def test_invite_create_and_accept_flow():
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
        owner_token = reg_owner.json()["token"]
        tenant_id = reg_owner.json()["tenant_id"]

        invited = client.post(
            "/api/members/invite",
            json={
                "email": "new.user@example.com",
                "role": "operator",
                "expires_in_hours": 24,
            },
            headers={
                "Authorization": f"Bearer {owner_token}",
                "X-Tenant-Id": str(tenant_id),
            },
        )
        assert invited.status_code == 200
        invite_payload = invited.json()
        invite_token = invite_payload["invite_token"]
        assert invite_token

        accepted = client.post(
            "/api/auth/accept-invite",
            json={
                "token": invite_token,
                "password": "NewStrongPass123!",
                "full_name": "New User",
            },
        )
        assert accepted.status_code == 200
        accepted_payload = accepted.json()
        assert accepted_payload["tenant_id"] == tenant_id

        login = client.post(
            "/api/auth/login",
            json={
                "email": "new.user@example.com",
                "password": "NewStrongPass123!",
                "tenant_slug": reg_owner.json()["tenant_slug"],
            },
        )
        assert login.status_code == 200

        reuse = client.post(
            "/api/auth/accept-invite",
            json={
                "token": invite_token,
                "password": "AnotherStrongPass123!",
            },
        )
        assert reuse.status_code == 404
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_ui_team_invite_is_disabled_and_accept_page_works(monkeypatch):
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
        owner_token = reg_owner.json()["token"]

        invite_ui = client.post(
            "/ui/team/invite",
            data={
                "email": "invitee@example.com",
                "role": "operator",
                "expires_in_hours": "24",
            },
            headers={"Authorization": f"Bearer {owner_token}"},
            follow_redirects=False,
        )
        assert invite_ui.status_code == 404

        accept_page = client.get("/ui/accept-invite")
        assert accept_page.status_code == 200
        assert "Принять приглашение" in accept_page.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_ui_accept_invite_sets_cookie_and_me_works():
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
        owner_token = reg_owner.json()["token"]
        tenant_id = reg_owner.json()["tenant_id"]

        invited = client.post(
            "/api/members/invite",
            json={
                "email": "cookie.user@example.com",
                "role": "operator",
                "expires_in_hours": 24,
            },
            headers={
                "Authorization": f"Bearer {owner_token}",
                "X-Tenant-Id": str(tenant_id),
            },
        )
        assert invited.status_code == 200
        invite_token = invited.json()["invite_token"]

        accepted = client.post(
            "/ui/accept-invite",
            data={
                "token": invite_token,
                "password": "CookiePass123!",
                "full_name": "Cookie User",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        set_cookie = accepted.headers.get("set-cookie", "")
        assert "auth_session=" in set_cookie

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == "cookie.user@example.com"
    finally:
        app.dependency_overrides.clear()
        db.close()

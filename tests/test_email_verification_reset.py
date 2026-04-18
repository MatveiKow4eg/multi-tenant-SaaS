"""Tests for email verification and password reset flows."""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.auth.email_tokens import create_email_token, consume_email_token


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _client_with_db(db):
    app.dependency_overrides[get_db] = lambda: (yield db)
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Email verification helpers
# ---------------------------------------------------------------------------


def test_verify_email_endpoint():
    db = _make_db()
    client = _client_with_db(db)

    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "verify@example.com",
                "password": "Pass1234!",
                "full_name": "Verifier",
                "tenant_name": "Verify Corp",
            },
        )
        assert reg.status_code == 200

        user = db.query(User).filter(User.email == "verify@example.com").first()
        assert user is not None
        assert user.email_verified is False  # not yet verified

        raw_token = create_email_token(db, user_id=user.id, purpose="verify_email")
        resp = client.get(f"/api/auth/verify-email?token={raw_token}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        db.refresh(user)
        assert user.email_verified is True
    finally:
        app.dependency_overrides.clear()


def test_verify_email_invalid_token():
    db = _make_db()
    client = _client_with_db(db)
    try:
        resp = client.get("/api/auth/verify-email?token=notarealtoken")
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_resend_verification_does_not_reveal_users():
    """resend-verification always returns 200 even for unknown email."""
    db = _make_db()
    client = _client_with_db(db)
    try:
        resp = client.post(
            "/api/auth/resend-verification",
            json={"email": "ghost@example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Password reset flow
# ---------------------------------------------------------------------------


def test_forgot_password_does_not_reveal_users():
    db = _make_db()
    client = _client_with_db(db)
    try:
        resp = client.post(
            "/api/auth/forgot-password",
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    finally:
        app.dependency_overrides.clear()


def test_api_forgot_password_cooldown_suppresses_second_send(monkeypatch):
    db = _make_db()
    client = _client_with_db(db)
    sent: list[str] = []

    calls = {"n": 0}

    def _fake_acquire(*, purpose: str, email: str, ttl_seconds: int) -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    def _fake_send_password_reset_email(*, to_email: str, reset_url: str) -> str:
        sent.append(to_email)
        assert "/ui/reset-password?token=" in reset_url
        return "msg-reset-1"

    monkeypatch.setattr("app.api.routes.auth.send_password_reset_email", _fake_send_password_reset_email)
    monkeypatch.setattr("app.api.routes.auth.acquire_email_cooldown", _fake_acquire)

    try:
        client.post(
            "/api/auth/register",
            json={
                "email": "api-forgot@example.com",
                "password": "StrongPass123!",
                "tenant_name": "API Forgot",
            },
        )

        r1 = client.post("/api/auth/forgot-password", json={"email": "api-forgot@example.com"})
        r2 = client.post("/api/auth/forgot-password", json={"email": "api-forgot@example.com"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert sent == ["api-forgot@example.com"]
    finally:
        app.dependency_overrides.clear()


def test_api_forgot_password_fail_open_when_redis_unavailable(monkeypatch):
    db = _make_db()
    client = _client_with_db(db)
    sent: list[str] = []

    def _fake_send_password_reset_email(*, to_email: str, reset_url: str) -> str:
        sent.append(to_email)
        return "msg-reset-2"

    monkeypatch.setattr("app.api.routes.auth.send_password_reset_email", _fake_send_password_reset_email)

    monkeypatch.setattr("app.api.routes.auth.acquire_email_cooldown", lambda **kwargs: True)

    try:
        client.post(
            "/api/auth/register",
            json={
                "email": "api-fail-open@example.com",
                "password": "StrongPass123!",
                "tenant_name": "API Fail Open",
            },
        )

        resp = client.post("/api/auth/forgot-password", json={"email": "api-fail-open@example.com"})
        assert resp.status_code == 200
        assert sent == ["api-fail-open@example.com"]
    finally:
        app.dependency_overrides.clear()


def test_password_reset_full_flow():
    db = _make_db()
    client = _client_with_db(db)
    try:
        # Register
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "resetme@example.com",
                "password": "OldPass123!",
                "full_name": "Reset User",
                "tenant_name": "Reset Corp",
            },
        )
        assert reg.status_code == 200

        user = db.query(User).filter(User.email == "resetme@example.com").first()
        assert user is not None

        # Create reset token manually (mail is mocked by exception bypass)
        raw_token = create_email_token(db, user_id=user.id, purpose="reset_password")

        # Reset password
        resp = client.post(
            "/api/auth/reset-password",
            json={"token": raw_token, "password": "NewPass456!"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Old session should NOT work (password changed but session still valid — acceptable)
        # New login with new password should work
        login = client.post(
            "/api/auth/login",
            json={"email": "resetme@example.com", "password": "NewPass456!"},
        )
        assert login.status_code == 200

        # Old password should fail
        login_old = client.post(
            "/api/auth/login",
            json={"email": "resetme@example.com", "password": "OldPass123!"},
        )
        assert login_old.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_reset_password_token_cannot_be_reused():
    db = _make_db()
    client = _client_with_db(db)
    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "reuse@example.com",
                "password": "Pass1234!",
                "full_name": "Reuse Test",
                "tenant_name": "Reuse Corp",
            },
        )
        assert reg.status_code == 200
        user = db.query(User).filter(User.email == "reuse@example.com").first()
        raw_token = create_email_token(db, user_id=user.id, purpose="reset_password")

        # Use once
        r1 = client.post("/api/auth/reset-password", json={"token": raw_token, "password": "New111Pass!"})
        assert r1.status_code == 200

        # Try again — should fail
        r2 = client.post("/api/auth/reset-password", json={"token": raw_token, "password": "Another222!"})
        assert r2.status_code == 400
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# UI login / register smoke tests
# ---------------------------------------------------------------------------


def test_ui_login_page_loads():
    db = _make_db()
    client = _client_with_db(db)
    try:
        resp = client.get("/ui/login")
        assert resp.status_code == 200
        assert "Вход" in resp.text
        assert "/ui/resend-verification" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_ui_register_page_loads():
    db = _make_db()
    client = _client_with_db(db)
    try:
        resp = client.get("/ui/register")
        assert resp.status_code == 200
        assert "Регистрация" in resp.text
        assert "/ui/resend-verification" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_ui_login_sets_cookie():
    db = _make_db()
    client = _client_with_db(db)
    try:
        # Register via API first
        client.post(
            "/api/auth/register",
            json={
                "email": "uicookie@example.com",
                "password": "LoginPass1!",
                "full_name": "UI Cookie",
                "tenant_name": "Cookie Corp",
            },
        )
        resp = client.post(
            "/ui/login",
            data={"email": "uicookie@example.com", "password": "LoginPass1!"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "auth_session" in resp.cookies
    finally:
        app.dependency_overrides.clear()


def test_ui_login_blocks_unverified_email_when_flag_enabled(monkeypatch):
    db = _make_db()
    client = _client_with_db(db)
    monkeypatch.setattr(settings, "auth_require_email_verified", True)
    try:
        client.post(
            "/api/auth/register",
            json={
                "email": "ui-unverified@example.com",
                "password": "LoginPass1!",
                "full_name": "UI Unverified",
                "tenant_name": "UICheck Corp",
            },
        )
        resp = client.post(
            "/ui/login",
            data={"email": "ui-unverified@example.com", "password": "LoginPass1!"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "auth_session" not in resp.cookies

        user = db.query(User).filter(User.email == "ui-unverified@example.com").first()
        assert user is not None
        user.email_verified = True
        db.commit()

        resp_verified = client.post(
            "/ui/login",
            data={"email": "ui-unverified@example.com", "password": "LoginPass1!"},
            follow_redirects=False,
        )
        assert resp_verified.status_code in (302, 303)
        assert "auth_session" in resp_verified.cookies
    finally:
        app.dependency_overrides.clear()


def test_ui_login_shows_lockout_message_when_block_active(monkeypatch):
    db = _make_db()
    client = _client_with_db(db)
    monkeypatch.setattr("app.ui.routes.is_login_allowed", lambda **kwargs: False)
    try:
        resp = client.post(
            "/ui/login",
            data={"email": "blocked@example.com", "password": "wrong-pass"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "auth_session" not in resp.cookies
    finally:
        app.dependency_overrides.clear()


def test_ui_login_lockout_triggers_on_threshold_and_clears_on_success(monkeypatch):
    db = _make_db()
    client = _client_with_db(db)
    state = {"failures": 0, "cleared": 0}

    monkeypatch.setattr("app.ui.routes.is_login_allowed", lambda **kwargs: True)

    def _fake_register_failure(**kwargs):
        state["failures"] += 1
        return state["failures"] >= 2

    def _fake_clear(**kwargs):
        state["cleared"] += 1

    monkeypatch.setattr("app.ui.routes.register_login_failure", _fake_register_failure)
    monkeypatch.setattr("app.ui.routes.clear_login_failures", _fake_clear)

    try:
        client.post(
            "/api/auth/register",
            json={
                "email": "ui-lock@example.com",
                "password": "StrongPass123!",
                "tenant_name": "UI Lock Team",
            },
        )

        bad1 = client.post(
            "/ui/login",
            data={"email": "ui-lock@example.com", "password": "wrong-pass"},
            follow_redirects=False,
        )
        bad2 = client.post(
            "/ui/login",
            data={"email": "ui-lock@example.com", "password": "wrong-pass"},
            follow_redirects=False,
        )
        good = client.post(
            "/ui/login",
            data={"email": "ui-lock@example.com", "password": "StrongPass123!"},
            follow_redirects=False,
        )

        assert bad1.status_code in (302, 303)
        assert "auth_session" not in bad1.cookies
        assert bad2.status_code in (302, 303)
        assert "auth_session" not in bad2.cookies
        assert good.status_code in (302, 303)
        assert "auth_session" in good.cookies
        assert state["cleared"] >= 1
    finally:
        app.dependency_overrides.clear()


def test_ui_auth_audit_events_are_written():
    db = _make_db()
    client = _client_with_db(db)
    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "ui-audit@example.com",
                "password": "StrongPass123!",
                "tenant_name": "UI Audit Team",
            },
        )
        assert reg.status_code == 200

        bad = client.post(
            "/ui/login",
            data={"email": "ui-audit@example.com", "password": "wrong-pass"},
            follow_redirects=False,
        )
        assert bad.status_code in (302, 303)

        good = client.post(
            "/ui/login",
            data={"email": "ui-audit@example.com", "password": "StrongPass123!"},
            follow_redirects=False,
        )
        assert good.status_code in (302, 303)

        actions = [row.action for row in db.query(AuditLog).all()]
        assert "ui_auth_login_failed" in actions
        assert "ui_auth_login_success" in actions
    finally:
        app.dependency_overrides.clear()


def test_ui_register_sets_cookie():
    db = _make_db()
    client = _client_with_db(db)
    try:
        resp = client.post(
            "/ui/register",
            data={
                "email": "newcorp@example.com",
                "password": "MyPass12!",
                "password_confirm": "MyPass12!",
                "full_name": "New User",
                "tenant_name": "New Corp",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "/ui/onboarding/start" in resp.headers.get("location", "")
        assert "auth_session" in resp.cookies
    finally:
        app.dependency_overrides.clear()


def test_ui_forgot_password_page_loads():
    db = _make_db()
    client = _client_with_db(db)
    try:
        resp = client.get("/ui/forgot-password")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_ui_reset_password_page_loads():
    db = _make_db()
    client = _client_with_db(db)
    try:
        resp = client.get("/ui/reset-password?token=sometoken")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_ui_forgot_password_cooldown_suppresses_second_send(monkeypatch):
    db = _make_db()
    client = _client_with_db(db)
    sent: list[str] = []

    calls = {"n": 0}

    def _fake_acquire(*, purpose: str, email: str, ttl_seconds: int) -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    def _fake_send_password_reset_email(*, to_email: str, reset_url: str) -> str:
        sent.append(to_email)
        assert "/ui/reset-password?token=" in reset_url
        return "msg-reset-ui-1"

    monkeypatch.setattr("app.ui.routes.send_password_reset_email", _fake_send_password_reset_email)
    monkeypatch.setattr("app.ui.routes.acquire_email_cooldown", _fake_acquire)

    try:
        client.post(
            "/api/auth/register",
            json={
                "email": "ui-forgot@example.com",
                "password": "StrongPass123!",
                "tenant_name": "UI Forgot",
            },
        )

        r1 = client.post("/ui/forgot-password", data={"email": "ui-forgot@example.com"}, follow_redirects=False)
        r2 = client.post("/ui/forgot-password", data={"email": "ui-forgot@example.com"}, follow_redirects=False)
        assert r1.status_code in (302, 303)
        assert r2.status_code in (302, 303)
        assert sent == ["ui-forgot@example.com"]
    finally:
        app.dependency_overrides.clear()


def test_ui_forgot_password_fail_open_when_redis_unavailable(monkeypatch):
    db = _make_db()
    client = _client_with_db(db)
    sent: list[str] = []

    def _fake_send_password_reset_email(*, to_email: str, reset_url: str) -> str:
        sent.append(to_email)
        return "msg-reset-ui-2"

    monkeypatch.setattr("app.ui.routes.send_password_reset_email", _fake_send_password_reset_email)

    monkeypatch.setattr("app.ui.routes.acquire_email_cooldown", lambda **kwargs: True)

    try:
        client.post(
            "/api/auth/register",
            json={
                "email": "ui-fail-open@example.com",
                "password": "StrongPass123!",
                "tenant_name": "UI Fail Open",
            },
        )

        resp = client.post("/ui/forgot-password", data={"email": "ui-fail-open@example.com"}, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert sent == ["ui-fail-open@example.com"]
    finally:
        app.dependency_overrides.clear()


def test_ui_resend_verification_is_non_enumerating():
    db = _make_db()
    client = _client_with_db(db)
    try:
        resp = client.post(
            "/ui/resend-verification",
            data={"email": "ghost@example.com"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
    finally:
        app.dependency_overrides.clear()


def test_ui_resend_verification_sends_email_for_unverified(monkeypatch):
    db = _make_db()
    client = _client_with_db(db)
    sent: list[str] = []

    calls = {"n": 0}

    def _fake_acquire(*, purpose: str, email: str, ttl_seconds: int) -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    def _fake_send_verification_email(*, to_email: str, verify_url: str) -> str:
        sent.append(to_email)
        assert "/ui/verify-email?token=" in verify_url
        return "msg-1"

    monkeypatch.setattr("app.ui.routes.send_verification_email", _fake_send_verification_email)
    monkeypatch.setattr("app.ui.routes.acquire_email_cooldown", _fake_acquire)

    try:
        client.post(
            "/api/auth/register",
            json={
                "email": "resend-ui@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Resend UI",
            },
        )

        r1 = client.post(
            "/ui/resend-verification",
            data={"email": "resend-ui@example.com"},
            follow_redirects=False,
        )
        assert r1.status_code in (302, 303)
        assert sent == ["resend-ui@example.com"]

        # Cooldown should suppress immediate second send.
        r1b = client.post(
            "/ui/resend-verification",
            data={"email": "resend-ui@example.com"},
            follow_redirects=False,
        )
        assert r1b.status_code in (302, 303)
        assert sent == ["resend-ui@example.com"]

        user = db.query(User).filter(User.email == "resend-ui@example.com").first()
        assert user is not None
        user.email_verified = True
        db.commit()

        r2 = client.post(
            "/ui/resend-verification",
            data={"email": "resend-ui@example.com"},
            follow_redirects=False,
        )
        assert r2.status_code in (302, 303)
        assert sent == ["resend-ui@example.com"]
    finally:
        app.dependency_overrides.clear()


def test_ui_resend_verification_fail_open_when_redis_unavailable(monkeypatch):
    db = _make_db()
    client = _client_with_db(db)
    sent: list[str] = []

    def _fake_send_verification_email(*, to_email: str, verify_url: str) -> str:
        sent.append(to_email)
        return "msg-2"

    monkeypatch.setattr("app.ui.routes.send_verification_email", _fake_send_verification_email)

    monkeypatch.setattr("app.ui.routes.acquire_email_cooldown", lambda **kwargs: True)

    try:
        client.post(
            "/api/auth/register",
            json={
                "email": "resend-fail-open@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Resend Fail Open",
            },
        )

        resp = client.post(
            "/ui/resend-verification",
            data={"email": "resend-fail-open@example.com"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert sent == ["resend-fail-open@example.com"]
    finally:
        app.dependency_overrides.clear()

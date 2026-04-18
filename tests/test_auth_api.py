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


def _build_test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_register_me_logout_flow():
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "owner@example.com",
                "password": "StrongPass123!",
                "full_name": "Owner",
                "tenant_name": "Alpha Team",
            },
        )
        assert reg.status_code == 200
        token = reg.json()["token"]
        assert token

        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        me_payload = me.json()
        assert me_payload["email"] == "owner@example.com"
        assert len(me_payload["memberships"]) == 1
        assert me_payload["memberships"][0]["role"] == "owner"

        logout = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
        assert logout.status_code == 200
        assert logout.json()["ok"] is True

        me_after = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me_after.status_code == 401
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_register_without_tenant_name_generates_workspace_name():
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "autoname@example.com",
                "password": "StrongPass123!",
                "full_name": "Auto Name",
            },
        )
        assert reg.status_code == 200
        payload = reg.json()
        assert payload["tenant_slug"].startswith("autoname-workspace")
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_logout_all_revokes_all_user_sessions():
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "owner2@example.com",
                "password": "StrongPass123!",
                "full_name": "Owner 2",
                "tenant_name": "Beta Team",
            },
        )
        assert reg.status_code == 200
        token1 = reg.json()["token"]

        login2 = client.post(
            "/api/auth/login",
            json={"email": "owner2@example.com", "password": "StrongPass123!"},
        )
        assert login2.status_code == 200
        token2 = login2.json()["token"]

        logout_all = client.post("/api/auth/logout-all", headers={"Authorization": f"Bearer {token1}"})
        assert logout_all.status_code == 200
        assert logout_all.json()["ok"] is True
        assert logout_all.json()["revoked"] >= 2

        me1 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token1}"})
        me2 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token2}"})
        assert me1.status_code == 401
        assert me2.status_code == 401
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_login_with_tenant_slug():
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "ops@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Beta Team",
            },
        )
        assert reg.status_code == 200
        tenant_slug = reg.json()["tenant_slug"]

        login = client.post(
            "/api/auth/login",
            json={
                "email": "ops@example.com",
                "password": "StrongPass123!",
                "tenant_slug": tenant_slug,
            },
        )
        assert login.status_code == 200
        payload = login.json()
        assert payload["tenant_slug"] == tenant_slug
        assert payload["token"]
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_login_requires_verified_email_when_flag_enabled(monkeypatch):
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    monkeypatch.setattr(settings, "auth_require_email_verified", True)

    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "unverified@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Gamma Team",
            },
        )
        assert reg.status_code == 200

        login = client.post(
            "/api/auth/login",
            json={"email": "unverified@example.com", "password": "StrongPass123!"},
        )
        assert login.status_code == 403
        assert login.json()["detail"] == "email_not_verified"

        user = db.query(User).filter(User.email == "unverified@example.com").first()
        assert user is not None
        user.email_verified = True
        db.commit()

        login_verified = client.post(
            "/api/auth/login",
            json={"email": "unverified@example.com", "password": "StrongPass123!"},
        )
        assert login_verified.status_code == 200
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_api_login_returns_lockout_when_bruteforce_block_active(monkeypatch):
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    monkeypatch.setattr("app.api.routes.auth.is_login_allowed", lambda **kwargs: False)

    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "blocked@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Blocked Team",
            },
        )
        assert reg.status_code == 200
        tenant_id = reg.json()["tenant_id"]

        resp = client.post(
            "/api/auth/login",
            json={"email": "blocked@example.com", "password": "wrong-pass"},
        )
        assert resp.status_code == 429
        assert resp.json()["detail"] == "login_temporarily_locked"

        audit_row = (
            db.query(AuditLog)
            .filter(AuditLog.entity_type == "auth", AuditLog.action == "auth_login_blocked")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row is not None
        assert isinstance(audit_row.details, dict)
        assert audit_row.details.get("tenant_id") == tenant_id
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_api_login_lockout_triggers_on_threshold_and_clears_on_success(monkeypatch):
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    state = {"failures": 0, "cleared": 0}

    monkeypatch.setattr("app.api.routes.auth.is_login_allowed", lambda **kwargs: True)

    def _fake_register_failure(**kwargs):
        state["failures"] += 1
        return state["failures"] >= 2

    def _fake_clear(**kwargs):
        state["cleared"] += 1

    monkeypatch.setattr("app.api.routes.auth.register_login_failure", _fake_register_failure)
    monkeypatch.setattr("app.api.routes.auth.clear_login_failures", _fake_clear)

    try:
        client.post(
            "/api/auth/register",
            json={
                "email": "lockme@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Lock Team",
            },
        )

        bad1 = client.post(
            "/api/auth/login",
            json={"email": "lockme@example.com", "password": "wrong-pass"},
        )
        bad2 = client.post(
            "/api/auth/login",
            json={"email": "lockme@example.com", "password": "wrong-pass"},
        )
        good = client.post(
            "/api/auth/login",
            json={"email": "lockme@example.com", "password": "StrongPass123!"},
        )

        assert bad1.status_code == 401
        assert bad2.status_code == 429
        assert good.status_code == 200
        assert state["cleared"] >= 1
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_api_auth_audit_events_are_written():
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "audit-auth@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Audit Team",
            },
        )
        assert reg.status_code == 200
        token = reg.json()["token"]

        bad = client.post(
            "/api/auth/login",
            json={"email": "audit-auth@example.com", "password": "wrong-pass"},
        )
        assert bad.status_code == 401

        good = client.post(
            "/api/auth/login",
            json={"email": "audit-auth@example.com", "password": "StrongPass123!"},
        )
        assert good.status_code == 200

        out_all = client.post("/api/auth/logout-all", headers={"Authorization": f"Bearer {token}"})
        assert out_all.status_code == 200

        actions = [row.action for row in db.query(AuditLog).all()]
        assert "auth_login_failed" in actions
        assert "auth_login_success" in actions
        assert "auth_logout_all" in actions

        tenant_id = reg.json()["tenant_id"]
        failed_row = (
            db.query(AuditLog)
            .filter(AuditLog.entity_type == "auth", AuditLog.action == "auth_login_failed")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert failed_row is not None
        assert isinstance(failed_row.details, dict)
        assert failed_row.details.get("tenant_id") == tenant_id

        success_row = (
            db.query(AuditLog)
            .filter(AuditLog.entity_type == "auth", AuditLog.action == "auth_login_success")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert success_row is not None
        assert isinstance(success_row.details, dict)
        assert success_row.details.get("tenant_id") == tenant_id

        logout_all_row = (
            db.query(AuditLog)
            .filter(AuditLog.entity_type == "auth", AuditLog.action == "auth_logout_all")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert logout_all_row is not None
        assert isinstance(logout_all_row.details, dict)
        assert logout_all_row.details.get("tenant_id") == tenant_id
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_api_resend_verification_respects_cooldown(monkeypatch):
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    state = {"calls": 0, "sent": 0}

    def _fake_cooldown(**kwargs):
        state["calls"] += 1
        return state["calls"] == 1

    def _fake_send_verification_email(*, to_email: str, verify_url: str):
        state["sent"] += 1
        return "ok"

    try:
        reg = client.post(
            "/api/auth/register",
            json={
                "email": "cooldown@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Cooldown Team",
            },
        )
        assert reg.status_code == 200

        monkeypatch.setattr("app.api.routes.auth.acquire_email_cooldown", _fake_cooldown)
        monkeypatch.setattr("app.api.routes.auth.send_verification_email", _fake_send_verification_email)

        r1 = client.post("/api/auth/resend-verification", json={"email": "cooldown@example.com"})
        r2 = client.post("/api/auth/resend-verification", json={"email": "cooldown@example.com"})

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert state["sent"] == 1
    finally:
        app.dependency_overrides.clear()
        db.close()

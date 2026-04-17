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


def test_owner_can_add_and_update_tenant_member():
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
                "tenant_name": "Alpha Team",
            },
        )
        assert reg.status_code == 200
        token = reg.json()["token"]

        initial = client.get("/api/members", headers={"Authorization": f"Bearer {token}"})
        assert initial.status_code == 200
        assert len(initial.json()) == 1

        added = client.post(
            "/api/members",
            json={
                "email": "operator@example.com",
                "full_name": "Operator",
                "password": "StrongPass123!",
                "role": "operator",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert added.status_code == 200
        added_payload = added.json()
        assert added_payload["role"] == "operator"
        member_id = added_payload["id"]

        updated = client.patch(
            f"/api/members/{member_id}",
            json={"role": "manager", "status": "disabled"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert updated.status_code == 200
        updated_payload = updated.json()
        assert updated_payload["role"] == "manager"
        assert updated_payload["status"] == "disabled"
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_viewer_cannot_add_members_but_can_view_team_page(monkeypatch):
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
        owner_token = reg_owner.json()["token"]
        tenant_slug = reg_owner.json()["tenant_slug"]

        add_viewer = client.post(
            "/api/members",
            json={
                "email": "viewer@example.com",
                "password": "StrongPass123!",
                "role": "viewer",
            },
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert add_viewer.status_code == 200

        login_viewer = client.post(
            "/api/auth/login",
            json={
                "email": "viewer@example.com",
                "password": "StrongPass123!",
                "tenant_slug": tenant_slug,
            },
        )
        assert login_viewer.status_code == 200
        viewer_token = login_viewer.json()["token"]

        ui_team = client.get("/ui/team", headers={"Authorization": f"Bearer {viewer_token}"})
        assert ui_team.status_code == 200

        denied = client.post(
            "/api/members",
            json={
                "email": "new@example.com",
                "password": "StrongPass123!",
                "role": "operator",
            },
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert denied.status_code == 403
    finally:
        app.dependency_overrides.clear()
        db.close()

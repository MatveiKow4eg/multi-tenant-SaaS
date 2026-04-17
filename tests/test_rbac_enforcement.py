from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
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


class _DummyTaskResult:
    id = "task-1"


class _DummyTask:
    def delay(self, **kwargs):
        return _DummyTaskResult()


def test_mutating_api_requires_auth_when_rbac_enforced(monkeypatch):
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    monkeypatch.setattr(settings, "auth_enforce_rbac", True)
    monkeypatch.setattr("app.api.routes.companies.run_finder", _DummyTask())
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        no_auth = client.post(
            "/api/companies/finder/run",
            json={"countries": ["Lithuania"], "results_per_query": 5},
            headers={"X-Tenant-Id": "1"},
        )
        assert no_auth.status_code == 401

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
        tenant_id = reg.json()["tenant_id"]

        with_auth = client.post(
            "/api/companies/finder/run",
            json={"countries": ["Lithuania"], "results_per_query": 5},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Tenant-Id": str(tenant_id),
            },
        )
        assert with_auth.status_code == 202
    finally:
        app.dependency_overrides.clear()
        db.close()

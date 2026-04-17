from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.company import Company, CompanyStatus
from app.models.tenant import Tenant


def _build_test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _seed(db):
    tenant_1 = Tenant(slug="alpha", name="Alpha")
    tenant_2 = Tenant(slug="beta", name="Beta")
    db.add(tenant_1)
    db.add(tenant_2)
    db.flush()

    db.add(
        Company(
            tenant_id=tenant_1.id,
            domain="alpha.example",
            name="Alpha Co",
            status=CompanyStatus.new,
        )
    )
    db.add(
        Company(
            tenant_id=tenant_2.id,
            domain="beta.example",
            name="Beta Co",
            status=CompanyStatus.new,
        )
    )
    db.commit()

    return tenant_1.id, tenant_2.id


def test_list_companies_is_scoped_by_tenant_header():
    db = _build_test_session()
    tenant_1_id, tenant_2_id = _seed(db)

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        response = client.get("/api/companies", headers={"X-Tenant-Id": str(tenant_1_id)})
        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["domain"] == "alpha.example"

        response_other = client.get("/api/companies", headers={"X-Tenant-Id": str(tenant_2_id)})
        assert response_other.status_code == 200
        payload_other = response_other.json()
        assert len(payload_other) == 1
        assert payload_other[0]["domain"] == "beta.example"
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_get_company_returns_404_for_other_tenant():
    db = _build_test_session()
    tenant_1_id, tenant_2_id = _seed(db)

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        allowed = client.get("/api/companies/1", headers={"X-Tenant-Id": str(tenant_1_id)})
        assert allowed.status_code == 200

        denied = client.get("/api/companies/1", headers={"X-Tenant-Id": str(tenant_2_id)})
        assert denied.status_code == 404
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_research_batch_validates_tenant_ownership(monkeypatch):
    db = _build_test_session()
    tenant_1_id, tenant_2_id = _seed(db)

    class _DummyResult:
        id = "task-1"

    captured: dict[str, object] = {}

    class _DummyTask:
        def delay(self, **kwargs):
            captured.update(kwargs)
            return _DummyResult()

    def _override_db():
        try:
            yield db
        finally:
            pass

    monkeypatch.setattr("app.api.routes.companies.run_research_batch", _DummyTask())
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        denied = client.post(
            "/api/companies/research-qualify/batch",
            json={"company_ids": [2]},
            headers={"X-Tenant-Id": str(tenant_1_id)},
        )
        assert denied.status_code == 404

        allowed = client.post(
            "/api/companies/research-qualify/batch",
            json={"company_ids": [1]},
            headers={"X-Tenant-Id": str(tenant_1_id)},
        )
        assert allowed.status_code == 202
        assert captured["company_ids"] == [1]
        assert captured["tenant_id"] == tenant_1_id

        allowed_other = client.post(
            "/api/companies/research-qualify/batch",
            json={"company_ids": [2]},
            headers={"X-Tenant-Id": str(tenant_2_id)},
        )
        assert allowed_other.status_code == 202
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_list_companies_uses_tenant_from_session_token():
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
        alpha_tenant_id = reg.json()["tenant_id"]

        beta_tenant = Tenant(slug="beta", name="Beta")
        db.add(beta_tenant)
        db.flush()

        db.add(Company(tenant_id=alpha_tenant_id, domain="alpha.example", status=CompanyStatus.new))
        db.add(Company(tenant_id=beta_tenant.id, domain="beta.example", status=CompanyStatus.new))
        db.commit()

        response = client.get(
            "/api/companies",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["domain"] == "alpha.example"
    finally:
        app.dependency_overrides.clear()
        db.close()

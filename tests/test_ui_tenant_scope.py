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


def test_ui_companies_is_scoped_by_tenant_header(monkeypatch):
    db = _build_test_session()
    tenant_1_id, tenant_2_id = _seed(db)

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
        response_1 = client.get("/ui/companies", headers={"X-Tenant-Id": str(tenant_1_id)})
        assert response_1.status_code == 200
        assert "alpha.example" in response_1.text
        assert "beta.example" not in response_1.text

        response_2 = client.get("/ui/companies", headers={"X-Tenant-Id": str(tenant_2_id)})
        assert response_2.status_code == 200
        assert "beta.example" in response_2.text
        assert "alpha.example" not in response_2.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_ui_company_detail_404_for_other_tenant(monkeypatch):
    db = _build_test_session()
    tenant_1_id, tenant_2_id = _seed(db)

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
        own = client.get("/ui/companies/1", headers={"X-Tenant-Id": str(tenant_1_id)})
        assert own.status_code == 200

        denied = client.get("/ui/companies/1", headers={"X-Tenant-Id": str(tenant_2_id)})
        assert denied.status_code == 404
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_ui_companies_uses_tenant_from_session_token(monkeypatch):
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
            "/ui/companies",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert "alpha.example" in response.text
        assert "beta.example" not in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()

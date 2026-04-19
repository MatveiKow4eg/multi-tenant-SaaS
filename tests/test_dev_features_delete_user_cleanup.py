from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.company import Company, CompanyStatus
from app.models.sender_domain import SenderDomain
from app.models.user import User
from app.services.sender_domains import create_sender_domain_profile


def _build_test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_dev_delete_user_also_deletes_companies_and_domains(monkeypatch):
    db = _build_test_session()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    monkeypatch.setattr("app.ui.routes._is_feature_enabled", lambda name: name == "DELETE_USERS")

    try:
        reg_owner = client.post(
            "/api/auth/register",
            json={
                "email": "owner.cleanup@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Cleanup Tenant",
            },
        )
        assert reg_owner.status_code == 200

        token = reg_owner.json()["token"]
        tenant_id = reg_owner.json()["tenant_id"]
        client.cookies.set("auth_session", token)

        owner = db.query(User).filter(User.email == "owner.cleanup@example.com").first()
        assert owner is not None
        owner_id = owner.id

        company = Company(
            tenant_id=tenant_id,
            domain="cleanup-example.com",
            name="Cleanup Example",
            status=CompanyStatus.new,
        )
        db.add(company)
        create_sender_domain_profile(db=db, tenant_id=tenant_id, domain="cleanup-example.com")
        db.add(
            AuditLog(
                tenant_id=tenant_id,
                action="onboarding_completed",
                entity_type="tenant",
                entity_id=tenant_id,
                details={"source": "test"},
            )
        )
        db.commit()

        dev_page = client.get("/ui/dev-features", follow_redirects=False)
        assert dev_page.status_code == 200
        csrf_token = client.cookies.get("csrf_token")
        assert csrf_token

        resp = client.post(
            "/ui/dev-features/delete-user",
            data={
                "user_id": str(owner.id),
                "membership_tenant_id": str(tenant_id),
            },
            headers={"X-CSRF-Token": csrf_token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/ui/dev-features" in (resp.headers.get("location") or "")

        company_after = db.query(Company).filter(Company.tenant_id == tenant_id).all()
        domain_after = db.query(SenderDomain).filter(SenderDomain.tenant_id == tenant_id).all()
        user_after = db.query(User).filter(User.id == owner_id).first()

        assert company_after == []
        assert domain_after == []
        assert user_after is None
    finally:
        app.dependency_overrides.clear()
        db.close()

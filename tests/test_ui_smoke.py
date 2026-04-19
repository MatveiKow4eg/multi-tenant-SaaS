from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.campaign import Campaign, CampaignStatus
from app.models.company import Company, CompanyStatus
from app.models.contact import Contact
from app.models.audit_log import AuditLog
from app.models.message import Message, MessageDirection
from app.models.sender_domain import SenderDomain


def _build_test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _seed(db, tenant_id: int):
    company = Company(
        tenant_id=tenant_id,
        domain="example.lt",
        name="Example LT",
        country="Lithuania",
        industry="metal fabrication",
        score=0.92,
        status=CompanyStatus.qualified,
        qualification_result={"is_relevant": True, "score": 0.92, "signals": ["cnc", "welding"]},
    )
    db.add(company)
    db.flush()

    contact = Contact(
        company_id=company.id,
        email="ops@example.lt",
        role="operations",
        confidence=0.9,
        source_url="https://example.lt/contacts",
    )
    db.add(contact)
    db.flush()

    campaign = Campaign(
        company_id=company.id,
        contact_id=contact.id,
        status=CampaignStatus.active,
        language="lt",
        brief="test brief",
        step=0,
        has_reply=False,
    )
    db.add(campaign)
    db.flush()

    db.add(
        Message(
            campaign_id=campaign.id,
            direction=MessageDirection.outbound,
            subject="Partnership inquiry",
            body="Hello from test",
            step=0,
            sent_at=datetime.utcnow(),
        )
    )

    db.add(
        AuditLog(
            entity_type="auth",
            entity_id=1,
            action="ui_auth_login_failed",
            details={"email": "seed@example.com"},
            reason="invalid_credentials",
        )
    )
    db.commit()


def test_ui_smoke_pages(monkeypatch):
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
        tenant_id = reg_owner.json()["tenant_id"]
        client.cookies.set("auth_session", auth_token)

        _seed(db, tenant_id)

        response_dashboard = client.get("/ui")
        response_companies = client.get("/ui/companies")
        response_company = client.get("/ui/companies/1")
        response_campaigns = client.get("/ui/campaigns")
        response_messages = client.get("/ui/messages")
        response_operations = client.get("/ui/operations?auth_action=ui_auth_login_failed")

        assert response_dashboard.status_code == 200
        assert response_companies.status_code == 200
        assert response_company.status_code == 200
        assert response_campaigns.status_code == 200
        assert response_messages.status_code == 200
        assert response_operations.status_code == 200
        assert "ui_auth_login_failed" in response_operations.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_ui_static_css_is_public_without_auth():
    client = TestClient(app)

    response = client.get("/ui-static/style.css", follow_redirects=False)

    assert response.status_code == 200
    assert "text/css" in response.headers.get("content-type", "")


def test_new_tenant_is_redirected_to_onboarding_until_setup():
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
                "email": "fresh-owner@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Fresh Team",
            },
        )
        assert reg_owner.status_code == 200

        token = reg_owner.json()["token"]
        client.cookies.set("auth_session", token)

        blocked = client.get("/ui/companies", follow_redirects=False)
        assert blocked.status_code in (302, 303)
        assert blocked.headers.get("location") == "/ui/onboarding/start"

        onboarding = client.get("/ui/onboarding/start", follow_redirects=False)
        assert onboarding.status_code == 200
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_onboarding_allows_changing_company_website_after_auto_domain_creation():
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
                "email": "change-domain@example.com",
                "password": "StrongPass123!",
                "tenant_name": "Change Domain Team",
            },
        )
        assert reg_owner.status_code == 200

        token = reg_owner.json()["token"]
        tenant_id = reg_owner.json()["tenant_id"]
        client.cookies.set("auth_session", token)

        onboarding = client.get("/ui/onboarding/start", follow_redirects=False)
        assert onboarding.status_code == 200
        csrf_token = client.cookies.get("csrf_token")
        assert csrf_token

        first_submit = client.post(
            "/ui/onboarding/start",
            data={
                "step": "1",
                "action": "next",
                "workspace_name": "Change Domain Team",
                "company_website": "https://www.first-example.com",
                "company_name": "First Example",
                "sender_name": "Owner",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert first_submit.status_code == 200
        assert first_submit.json()["success"] is True

        sender_domain = db.query(SenderDomain).filter(SenderDomain.tenant_id == tenant_id).first()
        assert sender_domain is not None
        assert sender_domain.domain == "first-example.com"

        second_submit = client.post(
            "/ui/onboarding/start",
            data={
                "step": "1",
                "action": "next",
                "workspace_name": "Change Domain Team",
                "company_website": "https://www.second-example.com",
                "company_name": "Second Example",
                "sender_name": "Owner",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert second_submit.status_code == 200
        second_payload = second_submit.json()
        assert second_payload["success"] is True
        assert second_payload["open_domain_id"] == sender_domain.id

        db.expire_all()
        updated_sender_domain = db.query(SenderDomain).filter(SenderDomain.tenant_id == tenant_id).first()
        assert updated_sender_domain is not None
        assert updated_sender_domain.id == sender_domain.id
        assert updated_sender_domain.domain == "second-example.com"
        assert len(updated_sender_domain.dns_records) > 0

        company = db.query(Company).filter(Company.tenant_id == tenant_id).first()
        assert company is None
    finally:
        app.dependency_overrides.clear()
        db.close()

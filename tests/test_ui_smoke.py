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
    company = Company(
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
    _seed(db)

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

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
from app.models.handoff import Handoff
from app.models.message import Message, MessageDirection
from app.models.reply import Reply
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
    tenant_a = Tenant(slug="alpha", name="Alpha")
    tenant_b = Tenant(slug="beta", name="Beta")
    db.add(tenant_a)
    db.add(tenant_b)
    db.flush()

    company_a = Company(tenant_id=tenant_a.id, domain="alpha.example", status=CompanyStatus.qualified)
    company_b = Company(tenant_id=tenant_b.id, domain="beta.example", status=CompanyStatus.qualified)
    db.add(company_a)
    db.add(company_b)
    db.flush()

    contact_a = Contact(company_id=company_a.id, email="a@example.com")
    contact_b = Contact(company_id=company_b.id, email="b@example.com")
    db.add(contact_a)
    db.add(contact_b)
    db.flush()

    campaign_a = Campaign(company_id=company_a.id, contact_id=contact_a.id, status=CampaignStatus.active)
    campaign_b = Campaign(company_id=company_b.id, contact_id=contact_b.id, status=CampaignStatus.active)
    db.add(campaign_a)
    db.add(campaign_b)
    db.flush()

    outbound_a = Message(
        campaign_id=campaign_a.id,
        direction=MessageDirection.outbound,
        sent_at=datetime.utcnow(),
    )
    inbound_a = Message(
        campaign_id=campaign_a.id,
        direction=MessageDirection.inbound,
        received_at=datetime.utcnow(),
    )
    outbound_b = Message(
        campaign_id=campaign_b.id,
        direction=MessageDirection.outbound,
        sent_at=datetime.utcnow(),
    )
    db.add(outbound_a)
    db.add(inbound_a)
    db.add(outbound_b)
    db.flush()

    db.add(Reply(message_id=inbound_a.id, label="interested"))
    db.add(Handoff(campaign_id=campaign_a.id, company_id=company_a.id, label="warm", priority="high", needs_human=True))
    db.commit()

    return tenant_a.id, tenant_b.id


def test_kpi_is_scoped_by_tenant_header():
    db = _build_test_session()
    tenant_a_id, tenant_b_id = _seed(db)

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)

    try:
        kpi_a = client.get("/api/operations/analytics/kpi", headers={"X-Tenant-Id": str(tenant_a_id)})
        assert kpi_a.status_code == 200
        payload_a = kpi_a.json()
        assert payload_a["companies_found"] == 1
        assert payload_a["contacts_found"] == 1
        assert payload_a["emails_sent"] == 1
        assert payload_a["replies_received"] == 1
        assert payload_a["warm_replies"] == 1

        kpi_b = client.get("/api/operations/analytics/kpi", headers={"X-Tenant-Id": str(tenant_b_id)})
        assert kpi_b.status_code == 200
        payload_b = kpi_b.json()
        assert payload_b["companies_found"] == 1
        assert payload_b["contacts_found"] == 1
        assert payload_b["emails_sent"] == 1
        assert payload_b["replies_received"] == 0
        assert payload_b["warm_replies"] == 0
    finally:
        app.dependency_overrides.clear()
        db.close()

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.models.audit_log import AuditLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.company import Company, CompanyStatus
from app.models.contact import Contact
from app.models.message import Message, MessageDirection
from app.models.sender_domain import SenderDomain
from app.services.sender_domains import create_sender_domain_profile
from app.tasks.mail_operator import send_campaign_step
from app.tasks.outreach import generate_outreach_for_company


def _build_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_outreach_generation_skips_when_score_below_threshold(monkeypatch):
    db = _build_db_session()
    TaskSession = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.outreach.SessionLocal", TaskSession)
    monkeypatch.setattr(settings, "outreach_min_qualification_score", 80.0)

    company = Company(
        domain="low-score.example",
        country="Lithuania",
        status=CompanyStatus.qualified,
        score=65,
    )
    db.add(company)
    db.flush()
    db.add(Contact(company_id=company.id, email="ops@low-score.example", role="operations", confidence=0.9))
    db.commit()

    result = generate_outreach_for_company.run(company_id=company.id)

    check_db = TaskSession()

    assert result["ok"] is False
    assert result["error"] == "qualification_score_below_min"
    assert check_db.query(Campaign).count() == 0

    row = (
        check_db.query(AuditLog)
        .filter(AuditLog.entity_type == "company", AuditLog.entity_id == company.id, AuditLog.action == "outreach_skipped")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert isinstance(row.details, dict)
    assert row.details.get("reason") == "qualification_score_below_min"



def test_mail_send_stops_campaign_when_score_drops_below_threshold(monkeypatch):
    db = _build_db_session()
    TaskSession = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.mail_operator.SessionLocal", TaskSession)
    monkeypatch.setattr(settings, "outreach_min_qualification_score", 70.0)

    company = Company(
        domain="score-drop.example",
        country="Lithuania",
        status=CompanyStatus.outreaching,
        score=50,
    )
    db.add(company)
    db.flush()

    contact = Contact(company_id=company.id, email="ops@score-drop.example", role="operations", confidence=0.8)
    db.add(contact)
    db.flush()

    campaign = Campaign(
        company_id=company.id,
        contact_id=contact.id,
        status=CampaignStatus.active,
        step=0,
        has_reply=False,
    )
    db.add(campaign)
    db.flush()

    db.add(
        Message(
            campaign_id=campaign.id,
            direction=MessageDirection.outbound,
            subject="Intro",
            body="Hello",
            step=0,
        )
    )
    db.commit()

    called = {"send": 0}

    def _fake_send_zone_email(**kwargs):
        called["send"] += 1
        return "<msg-id@example.com>"

    monkeypatch.setattr("app.tasks.mail_operator.send_zone_email", _fake_send_zone_email)

    result = send_campaign_step.run(campaign_id=campaign.id)

    check_db = TaskSession()

    assert result["ok"] is False
    assert result["error"] == "qualification_score_below_min"
    assert called["send"] == 0

    campaign_fresh = check_db.query(Campaign).filter(Campaign.id == campaign.id).first()
    assert campaign_fresh is not None
    assert campaign_fresh.status == CampaignStatus.stopped

    row = (
        check_db.query(AuditLog)
        .filter(AuditLog.entity_type == "campaign", AuditLog.entity_id == campaign.id, AuditLog.action == "campaign_stopped")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert isinstance(row.details, dict)
    assert row.details.get("reason") == "qualification_score_below_min"


def test_mail_send_uses_verified_sender_domain_identity(monkeypatch):
    db = _build_db_session()
    TaskSession = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.mail_operator.SessionLocal", TaskSession)
    monkeypatch.setattr(settings, "outreach_min_qualification_score", 10.0)

    company = Company(
        tenant_id=77,
        domain="prospect.example",
        country="Lithuania",
        status=CompanyStatus.outreaching,
        score=95,
    )
    db.add(company)
    db.flush()

    sender_domain = create_sender_domain_profile(db=db, tenant_id=77, domain="sender.example")
    sender_domain.send_enabled = True
    sender_domain.status = "verified"
    sender_domain.spf_status = "verified"
    sender_domain.dkim_status = "verified"
    sender_domain.dmarc_status = "verified"

    contact = Contact(company_id=company.id, email="ops@prospect.example", role="operations", confidence=0.8)
    db.add(contact)
    db.flush()

    campaign = Campaign(
        company_id=company.id,
        contact_id=contact.id,
        status=CampaignStatus.active,
        step=0,
        has_reply=False,
    )
    db.add(campaign)
    db.flush()

    db.add(
        Message(
            campaign_id=campaign.id,
            direction=MessageDirection.outbound,
            subject="Intro",
            body="Hello",
            step=0,
        )
    )
    db.commit()

    captured = {}

    def _fake_send_zone_email(**kwargs):
        captured.update(kwargs)
        return "<msg-id@example.com>"

    monkeypatch.setattr("app.tasks.mail_operator.send_zone_email", _fake_send_zone_email)

    result = send_campaign_step.run(campaign_id=campaign.id)

    assert result["ok"] is True
    assert captured["from_email"] == "hello@sender.example"
    assert captured["dkim_selector"] in {"s1", "s2"}
    assert captured["dkim_domain"] == "sender.example"
    assert captured["dkim_private_key_pem"].startswith(b"-----BEGIN")

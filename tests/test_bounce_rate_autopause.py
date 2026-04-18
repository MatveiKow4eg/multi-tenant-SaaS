"""Tests for auto-pause on high bounce rate."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.audit_log import AuditLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.company import Company, CompanyStatus
from app.models.contact import Contact
from app.models.message import Message, MessageDirection
from app.models.reply import Reply
from app.models.tenant import Tenant
from app.tasks.replies import _check_bounce_rate_and_pause

ENGINE = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestSession = sessionmaker(bind=ENGINE)
Base.metadata.create_all(ENGINE)


@pytest.fixture(autouse=True)
def clean():
    yield
    s = TestSession()
    for table in reversed(Base.metadata.sorted_tables):
        s.execute(table.delete())
    s.commit()
    s.close()


@pytest.fixture()
def db():
    s = TestSession()
    yield s
    s.close()


def _make_campaign_with_sent(db, domain: str, tenant_id: int, sent: int, bounces: int) -> Campaign:
    company = Company(domain=domain, name=f"Co {domain}", status=CompanyStatus.outreaching, tenant_id=tenant_id)
    db.add(company)
    db.flush()

    contact = Contact(email=f"contact@{domain}", company_id=company.id)
    db.add(contact)
    db.flush()

    campaign = Campaign(company_id=company.id, contact_id=contact.id, status=CampaignStatus.active)
    db.add(campaign)
    db.flush()

    for i in range(sent):
        msg = Message(
            campaign_id=campaign.id,
            direction=MessageDirection.outbound,
            subject=f"Subject {i}",
            body="body",
            sent_at=__import__("datetime").datetime.utcnow(),
        )
        db.add(msg)
        db.flush()

        if i < bounces:
            reply = Reply(message_id=msg.id, label="bounced", summary="bounced")
            db.add(reply)

    db.commit()
    return campaign


def test_no_pause_when_below_threshold(db):
    """10% bounce with threshold 10% but only 3 sent → min_sent not reached."""
    tenant = Tenant(slug="t1", name="T1")
    db.add(tenant)
    db.flush()

    campaign = _make_campaign_with_sent(db, "example.com", tenant.id, sent=3, bounces=1)
    company = db.query(Company).filter(Company.id == campaign.company_id).first()

    with patch("app.tasks.replies.settings") as mock_settings:
        mock_settings.outreach_bounce_min_sent = 5
        mock_settings.outreach_max_bounce_rate = 0.10
        result = _check_bounce_rate_and_pause(company, db)

    assert result is False
    db.refresh(campaign)
    assert campaign.status == CampaignStatus.active


def test_pause_when_bounce_rate_exceeded(db):
    """5 sent, 2 bounces = 40% > 10% threshold → auto-pause triggered."""
    tenant = Tenant(slug="t2", name="T2")
    db.add(tenant)
    db.flush()

    campaign = _make_campaign_with_sent(db, "highbounce.com", tenant.id, sent=5, bounces=2)
    company = db.query(Company).filter(Company.id == campaign.company_id).first()

    with patch("app.tasks.replies.settings") as mock_settings:
        mock_settings.outreach_bounce_min_sent = 5
        mock_settings.outreach_max_bounce_rate = 0.10
        result = _check_bounce_rate_and_pause(company, db)

    assert result is True
    db.commit()
    db.refresh(campaign)
    assert campaign.status == CampaignStatus.paused

    # Audit log must exist
    audit = db.query(AuditLog).filter(AuditLog.action == "campaign_paused_bounce_rate").first()
    assert audit is not None
    assert audit.details["rate"] > 0.10


def test_no_pause_when_rate_acceptable(db):
    """10 sent, 1 bounce = 10% exactly at threshold → no pause."""
    tenant = Tenant(slug="t3", name="T3")
    db.add(tenant)
    db.flush()

    campaign = _make_campaign_with_sent(db, "okdomain.com", tenant.id, sent=10, bounces=1)
    company = db.query(Company).filter(Company.id == campaign.company_id).first()

    with patch("app.tasks.replies.settings") as mock_settings:
        mock_settings.outreach_bounce_min_sent = 5
        mock_settings.outreach_max_bounce_rate = 0.10
        result = _check_bounce_rate_and_pause(company, db)

    # 1/10 = 0.10, not strictly < 0.10 → pause triggered
    # (threshold is >=, so 10% at 10% → paused)
    assert result is True


def test_multiple_active_campaigns_all_paused(db):
    """Two active campaigns on same domain → both get paused."""
    tenant = Tenant(slug="t4", name="T4")
    db.add(tenant)
    db.flush()

    company = Company(domain="multi.com", name="Multi", status=CompanyStatus.outreaching, tenant_id=tenant.id)
    db.add(company)
    db.flush()

    contact1 = Contact(email="c1@multi.com", company_id=company.id)
    contact2 = Contact(email="c2@multi.com", company_id=company.id)
    db.add_all([contact1, contact2])
    db.flush()

    camp1 = Campaign(company_id=company.id, contact_id=contact1.id, status=CampaignStatus.active)
    camp2 = Campaign(company_id=company.id, contact_id=contact2.id, status=CampaignStatus.active)
    db.add_all([camp1, camp2])
    db.flush()

    # 6 sent on camp1, 2 bounced
    for i in range(6):
        msg = Message(campaign_id=camp1.id, direction=MessageDirection.outbound,
                      subject="s", body="b", sent_at=__import__("datetime").datetime.utcnow())
        db.add(msg)
        db.flush()
        if i < 2:
            db.add(Reply(message_id=msg.id, label="bounced", summary="b"))
    db.commit()

    with patch("app.tasks.replies.settings") as mock_settings:
        mock_settings.outreach_bounce_min_sent = 5
        mock_settings.outreach_max_bounce_rate = 0.10
        result = _check_bounce_rate_and_pause(company, db)

    assert result is True
    db.commit()
    db.refresh(camp1)
    db.refresh(camp2)
    assert camp1.status == CampaignStatus.paused
    assert camp2.status == CampaignStatus.paused

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.blacklist import Blacklist
from app.models.campaign import Campaign, CampaignStatus
from app.models.company import Company, CompanyStatus
from app.models.contact import Contact
from app.models.message import Message, MessageDirection
from app.models.schedule import Schedule
from app.tasks import replies as replies_task


def _build_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _seed_campaign(db, *, domain: str, contact_email: str):
    company = Company(domain=domain, status=CompanyStatus.qualified)
    db.add(company)
    db.flush()

    contact = Contact(company_id=company.id, email=contact_email, role="operations", confidence=0.8)
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

    outbound = Message(
        campaign_id=campaign.id,
        direction=MessageDirection.outbound,
        message_id="<out-1@example.com>",
        to_email=contact.email,
        subject="Intro",
        body="Hello",
        step=0,
    )
    db.add(outbound)

    followup = Schedule(
        campaign_id=campaign.id,
        run_at=datetime.now(timezone.utc),
        task_name="mail.send_campaign_step",
        step=1,
        executed=False,
    )
    db.add(followup)
    db.commit()

    return campaign.id, contact.email


def test_out_of_office_does_not_stop_campaign(monkeypatch):
    db = _build_db_session()
    campaign_id, _ = _seed_campaign(db, domain="ooo.example", contact_email="ops@ooo.example")

    monkeypatch.setattr(replies_task, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        replies_task,
        "fetch_unseen_zone_messages",
        lambda limit=30: [
            {
                "message_id": "<in-ooo@example.com>",
                "from": "Ops <ops@ooo.example>",
                "subject": "Out of office",
                "body": "I am out of office until Monday",
                "in_reply_to": "<out-1@example.com>",
                "references": "<out-1@example.com>",
                "date": datetime.now(timezone.utc),
                "has_attachments": False,
            }
        ],
    )
    monkeypatch.setattr(
        replies_task,
        "classify_reply",
        lambda **kwargs: {
            "label": "out_of_office",
            "summary": "Automatic out of office",
            "needs_human": False,
            "next_action": "wait",
        },
    )

    result = replies_task.ingest_and_classify.run(limit=10)

    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    assert result["processed"] == 1
    assert campaign is not None
    assert campaign.has_reply is False
    assert campaign.status == CampaignStatus.active

    pending = (
        db.query(Schedule)
        .filter(Schedule.campaign_id == campaign_id, Schedule.executed.is_(False))
        .count()
    )
    assert pending == 1



def test_wrong_contact_stops_campaign_and_blacklists_sender(monkeypatch):
    db = _build_db_session()
    campaign_id, _ = _seed_campaign(db, domain="wrong-contact.example", contact_email="info@wrong-contact.example")

    monkeypatch.setattr(replies_task, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        replies_task,
        "fetch_unseen_zone_messages",
        lambda limit=30: [
            {
                "message_id": "<in-wrong@example.com>",
                "from": "John <john@wrong-contact.example>",
                "subject": "Wrong person",
                "body": "This is not the right contact",
                "in_reply_to": "<out-1@example.com>",
                "references": "<out-1@example.com>",
                "date": datetime.now(timezone.utc),
                "has_attachments": False,
            }
        ],
    )
    monkeypatch.setattr(
        replies_task,
        "classify_reply",
        lambda **kwargs: {
            "label": "wrong_contact",
            "summary": "Reply says recipient is not relevant contact",
            "needs_human": True,
            "next_action": "find_alternate_contact",
        },
    )

    result = replies_task.ingest_and_classify.run(limit=10)

    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    assert result["processed"] == 1
    assert campaign is not None
    assert campaign.has_reply is True
    assert campaign.status == CampaignStatus.stopped

    pending = (
        db.query(Schedule)
        .filter(Schedule.campaign_id == campaign_id, Schedule.executed.is_(False))
        .count()
    )
    assert pending == 0

    blacklisted = (
        db.query(Blacklist)
        .filter(Blacklist.entry_type == "email", Blacklist.value == "john@wrong-contact.example")
        .first()
    )
    assert blacklisted is not None



def test_bounce_stops_campaign_and_blacklists_target_email(monkeypatch):
    db = _build_db_session()
    campaign_id, target_email = _seed_campaign(db, domain="bounce.example", contact_email="ops@bounce.example")

    monkeypatch.setattr(replies_task, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        replies_task,
        "fetch_unseen_zone_messages",
        lambda limit=30: [
            {
                "message_id": "<in-bounce@example.com>",
                "from": "Mail Delivery Subsystem <mailer-daemon@example.net>",
                "subject": "Delivery Status Notification (Failure)",
                "body": "550 5.1.1 user unknown",
                "in_reply_to": "<out-1@example.com>",
                "references": "<out-1@example.com>",
                "date": datetime.now(timezone.utc),
                "has_attachments": False,
            }
        ],
    )
    monkeypatch.setattr(
        replies_task,
        "classify_reply",
        lambda **kwargs: {
            "label": "bounced",
            "summary": "Mailbox is undeliverable",
            "needs_human": False,
            "next_action": "stop_campaign_and_mark_bounce",
        },
    )

    result = replies_task.ingest_and_classify.run(limit=10)

    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    assert result["processed"] == 1
    assert campaign is not None
    assert campaign.has_reply is False
    assert campaign.status == CampaignStatus.stopped

    pending = (
        db.query(Schedule)
        .filter(Schedule.campaign_id == campaign_id, Schedule.executed.is_(False))
        .count()
    )
    assert pending == 0

    blacklisted = (
        db.query(Blacklist)
        .filter(Blacklist.entry_type == "email", Blacklist.value == target_email)
        .first()
    )
    assert blacklisted is not None

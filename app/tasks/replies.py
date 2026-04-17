from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parseaddr

from app.db.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.blacklist import Blacklist
from app.models.campaign import Campaign, CampaignStatus
from app.models.company import Company
from app.models.contact import Contact
from app.models.handoff import Handoff
from app.models.message import Message, MessageDirection
from app.models.reply import Reply
from app.models.schedule import Schedule
from app.services.mail.zone_operator import fetch_unseen_zone_messages
from app.services.reply_analyst.analyzer import classify_reply
from app.worker.celery_app import celery_app


def _extract_refs(raw: str | None) -> list[str]:
    if not raw:
        return []
    return re.findall(r"<[^>]+>", raw)


def _find_campaign_for_inbound(from_email: str, in_reply_to: str | None, references: str | None, db) -> Campaign | None:
    if in_reply_to:
        msg = db.query(Message).filter(Message.message_id == in_reply_to).first()
        if msg:
            return db.query(Campaign).filter(Campaign.id == msg.campaign_id).first()

    ref_ids = _extract_refs(references)
    if ref_ids:
        msg = db.query(Message).filter(Message.message_id.in_(ref_ids)).first()
        if msg:
            return db.query(Campaign).filter(Campaign.id == msg.campaign_id).first()

    contact = db.query(Contact).filter(Contact.email == from_email.lower()).first()
    if contact:
        return (
            db.query(Campaign)
            .filter(Campaign.contact_id == contact.id)
            .order_by(Campaign.created_at.desc())
            .first()
        )

    return None


def _ensure_blacklist(email_value: str | None, domain_value: str | None, reason: str, db) -> None:
    if email_value:
        exists = db.query(Blacklist).filter(Blacklist.entry_type == "email", Blacklist.value == email_value).first()
        if not exists:
            db.add(Blacklist(entry_type="email", value=email_value, reason=reason))
    if domain_value:
        exists = db.query(Blacklist).filter(Blacklist.entry_type == "domain", Blacklist.value == domain_value).first()
        if not exists:
            db.add(Blacklist(entry_type="domain", value=domain_value, reason=reason))


@celery_app.task(name="reply.ingest_and_classify", bind=True, max_retries=2)
def ingest_and_classify(self, limit: int = 30) -> dict:
    db = SessionLocal()
    try:
        inbox = fetch_unseen_zone_messages(limit=limit)
        processed = 0
        warm = 0

        for item in inbox:
            message_id = item.get("message_id")
            if message_id:
                exists = db.query(Message).filter(Message.message_id == message_id).first()
                if exists:
                    continue

            from_email = parseaddr(item.get("from", ""))[1].lower()
            campaign = _find_campaign_for_inbound(
                from_email=from_email,
                in_reply_to=item.get("in_reply_to"),
                references=item.get("references"),
                db=db,
            )

            if not campaign:
                db.add(
                    AuditLog(
                        entity_type="message",
                        entity_id=None,
                        action="reply_unmatched_manual_review",
                        details={
                            "from": item.get("from"),
                            "subject": item.get("subject"),
                            "message_id": message_id,
                        },
                        reason="Inbound email could not be mapped to any campaign.",
                    )
                )
                continue

            inbound = Message(
                campaign_id=campaign.id,
                direction=MessageDirection.inbound,
                message_id=message_id,
                from_email=from_email or None,
                to_email=None,
                thread_reference=item.get("references") or item.get("in_reply_to"),
                subject=item.get("subject"),
                body=item.get("body") or "",
                step=campaign.step,
                has_attachments=bool(item.get("has_attachments")),
                received_at=item.get("date") or datetime.now(timezone.utc),
            )
            db.add(inbound)
            db.flush()

            cls = classify_reply(subject=inbound.subject or "", body=inbound.body or "")
            db.add(
                Reply(
                    message_id=inbound.id,
                    label=cls.get("label"),
                    summary=cls.get("summary"),
                    needs_human=bool(cls.get("needs_human")),
                    next_action=cls.get("next_action"),
                )
            )

            campaign.has_reply = True
            campaign.status = CampaignStatus.replied
            db.add(campaign)

            (
                db.query(Schedule)
                .filter(Schedule.campaign_id == campaign.id, Schedule.executed.is_(False))
                .update({Schedule.executed: True})
            )

            db.add(
                AuditLog(
                    entity_type="campaign",
                    entity_id=campaign.id,
                    action="reply_classified",
                    details={
                        "label": cls.get("label"),
                        "summary": cls.get("summary"),
                        "needs_human": cls.get("needs_human"),
                    },
                    reason="Reply Analyst classified inbound message.",
                )
            )

            company = db.query(Company).filter(Company.id == campaign.company_id).first()
            if cls.get("label") == "not_interested":
                _ensure_blacklist(
                    email_value=from_email or None,
                    domain_value=company.domain if company else None,
                    reason="Not interested / do not contact reply.",
                    db=db,
                )
                campaign.status = CampaignStatus.stopped
                db.add(campaign)

            warm_labels = {"interested", "ask_for_details", "send_rates"}
            if cls.get("label") in warm_labels or bool(cls.get("needs_human")):
                warm += 1
                priority = "high" if cls.get("label") in warm_labels else "medium"
                recommended_reply = (
                    "Prepare a short human response with details and propose call slots."
                    if cls.get("label") in {"interested", "ask_for_details", "send_rates"}
                    else "Review reply manually and decide next step."
                )
                db.add(
                    Handoff(
                        campaign_id=campaign.id,
                        company_id=company.id if company else None,
                        contact_id=campaign.contact_id,
                        label=cls.get("label") or "ask_for_details",
                        priority=priority,
                        needs_human=bool(cls.get("needs_human", True)),
                        summary=cls.get("summary"),
                        recommended_reply=recommended_reply,
                        payload={
                            "company": company.domain if company else None,
                            "from": from_email,
                            "subject": inbound.subject,
                            "message": (inbound.body or "")[:2000],
                            "reply_summary": cls.get("summary"),
                            "recommended_next_action": cls.get("next_action"),
                        },
                    )
                )
                db.add(
                    AuditLog(
                        entity_type="campaign",
                        entity_id=campaign.id,
                        action="warm_handoff",
                        details={
                            "company": company.domain if company else None,
                            "from": from_email,
                            "subject": inbound.subject,
                            "reply_summary": cls.get("summary"),
                            "recommended_next_action": cls.get("next_action"),
                            "priority": priority,
                        },
                        reason="Warm reply detected and handed off to human.",
                    )
                )

            processed += 1

        db.commit()
        return {"processed": processed, "warm": warm, "inbox_seen": len(inbox)}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=90)
    finally:
        db.close()

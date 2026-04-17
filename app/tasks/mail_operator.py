from __future__ import annotations

from datetime import datetime, timedelta, timezone
from sqlalchemy import func

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.company import Company
from app.models.contact import Contact
from app.models.message import Message, MessageDirection
from app.models.schedule import Schedule
from app.services.outreach.policy import get_blacklist_skip_reason, is_country_allowed_for_outreach
from app.services.mail.zone_operator import send_zone_email
from app.worker.celery_app import celery_app


def _has_pending_schedule(campaign_id: int, step: int, db) -> bool:
    return (
        db.query(Schedule)
        .filter(
            Schedule.campaign_id == campaign_id,
            Schedule.step == step,
            Schedule.executed.is_(False),
        )
        .first()
    ) is not None


def _defer_current_step(campaign_id: int, step: int | None, run_at: datetime, reason: str, db) -> None:
    if step is None:
        return
    if _has_pending_schedule(campaign_id=campaign_id, step=step, db=db):
        return
    db.add(
        Schedule(
            campaign_id=campaign_id,
            run_at=run_at,
            task_name="mail.send_campaign_step",
            step=step,
            executed=False,
        )
    )
    db.add(
        AuditLog(
            entity_type="campaign",
            entity_id=campaign_id,
            action="send_deferred",
            details={"step": step, "run_at": run_at.isoformat(), "reason": reason},
            reason="Mail send deferred due to rate limit.",
        )
    )


def _next_day_utc() -> datetime:
    now = datetime.now(timezone.utc)
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return next_day


@celery_app.task(name="mail.send_campaign_step", bind=True, max_retries=2)
def send_campaign_step(self, campaign_id: int) -> dict:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign:
            return {"ok": False, "error": "campaign_not_found", "campaign_id": campaign_id}

        if campaign.status not in {CampaignStatus.active, CampaignStatus.paused}:
            return {"ok": False, "error": "campaign_not_active", "campaign_id": campaign_id}

        if campaign.has_reply:
            campaign.status = CampaignStatus.replied
            db.add(campaign)
            db.commit()
            return {"ok": False, "error": "campaign_has_reply", "campaign_id": campaign_id}

        company = db.query(Company).filter(Company.id == campaign.company_id).first()
        contact = db.query(Contact).filter(Contact.id == campaign.contact_id).first() if campaign.contact_id else None
        if not company or not contact:
            return {"ok": False, "error": "campaign_data_missing", "campaign_id": campaign_id}

        if not is_country_allowed_for_outreach(company.country):
            campaign.status = CampaignStatus.stopped
            db.add(campaign)
            db.add(
                AuditLog(
                    entity_type="campaign",
                    entity_id=campaign.id,
                    action="campaign_stopped",
                    details={"reason": "country_not_allowed", "country": company.country},
                    reason="Skipped because country not allowed for outreach.",
                )
            )
            db.commit()
            return {"ok": False, "error": "country_not_allowed", "campaign_id": campaign_id}

        blacklist_reason = get_blacklist_skip_reason(
            company_domain=company.domain,
            contact_email=contact.email,
            db=db,
        )
        if blacklist_reason:
            campaign.status = CampaignStatus.stopped
            db.add(campaign)
            db.add(
                AuditLog(
                    entity_type="campaign",
                    entity_id=campaign.id,
                    action="campaign_stopped",
                    details={"reason": blacklist_reason},
                    reason="Domain or email is in blacklist.",
                )
            )
            db.commit()
            return {"ok": False, "error": blacklist_reason, "campaign_id": campaign_id}

        msg = (
            db.query(Message)
            .filter(
                Message.campaign_id == campaign.id,
                Message.direction == MessageDirection.outbound,
                Message.sent_at.is_(None),
            )
            .order_by(Message.step.asc().nullsfirst(), Message.id.asc())
            .first()
        )
        if not msg:
            campaign.status = CampaignStatus.completed
            db.add(campaign)
            db.commit()
            return {"ok": True, "campaign_id": campaign.id, "status": "completed"}

        last_sent = (
            db.query(Message)
            .filter(
                Message.campaign_id == campaign.id,
                Message.direction == MessageDirection.outbound,
                Message.sent_at.isnot(None),
            )
            .order_by(Message.sent_at.desc())
            .first()
        )

        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = (
            db.query(func.count(Message.id))
            .filter(
                Message.direction == MessageDirection.outbound,
                Message.sent_at.isnot(None),
                Message.sent_at >= start_of_day,
                Message.from_email == settings.zone_email,
            )
            .scalar()
            or 0
        )
        if sent_today >= settings.mail_daily_limit:
            _defer_current_step(
                campaign_id=campaign.id,
                step=msg.step,
                run_at=_next_day_utc(),
                reason="daily_limit",
                db=db,
            )
            db.commit()
            return {
                "ok": False,
                "error": "daily_limit_reached",
                "campaign_id": campaign.id,
                "sent_today": sent_today,
            }

        last_outbound_global = (
            db.query(Message)
            .filter(
                Message.direction == MessageDirection.outbound,
                Message.sent_at.isnot(None),
                Message.from_email == settings.zone_email,
            )
            .order_by(Message.sent_at.desc())
            .first()
        )
        if last_outbound_global and settings.mail_min_interval_seconds > 0:
            allowed_at = last_outbound_global.sent_at + timedelta(seconds=settings.mail_min_interval_seconds)
            if allowed_at > now:
                _defer_current_step(
                    campaign_id=campaign.id,
                    step=msg.step,
                    run_at=allowed_at,
                    reason="min_interval",
                    db=db,
                )
                db.commit()
                return {
                    "ok": False,
                    "error": "min_interval_not_elapsed",
                    "campaign_id": campaign.id,
                    "allowed_at": allowed_at.isoformat(),
                }

        message_id = send_zone_email(
            to_email=contact.email,
            subject=msg.subject or "Partnership inquiry",
            body=msg.body or "",
            in_reply_to=last_sent.message_id if last_sent else None,
        )

        msg.message_id = message_id
        msg.sent_at = now
        msg.from_email = settings.zone_email
        msg.to_email = contact.email
        msg.thread_reference = last_sent.message_id if last_sent else None
        if msg.step is not None:
            campaign.step = msg.step
        campaign.status = CampaignStatus.active

        db.add(
            AuditLog(
                entity_type="message",
                entity_id=msg.id,
                action="email_sent",
                details={
                    "campaign_id": campaign.id,
                    "step": msg.step,
                    "message_id": message_id,
                    "to": contact.email,
                },
                reason="Mail Operator sent outbound email via Zone SMTP.",
            )
        )

        if msg.step == 0:
            db.add(
                Schedule(
                    campaign_id=campaign.id,
                    run_at=now + timedelta(days=4),
                    task_name="mail.send_campaign_step",
                    step=1,
                    executed=False,
                )
            )
        elif msg.step == 1:
            db.add(
                Schedule(
                    campaign_id=campaign.id,
                    run_at=now + timedelta(days=6),
                    task_name="mail.send_campaign_step",
                    step=2,
                    executed=False,
                )
            )

        db.commit()
        return {"ok": True, "campaign_id": campaign.id, "message_id": message_id, "step": msg.step}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=90)
    finally:
        db.close()


@celery_app.task(name="mail.process_due_schedules")
def process_due_schedules(limit: int = 100) -> dict:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        due = (
            db.query(Schedule)
            .join(Campaign, Campaign.id == Schedule.campaign_id)
            .filter(Schedule.executed.is_(False), Schedule.run_at <= now, Campaign.has_reply.is_(False))
            .order_by(Schedule.run_at.asc())
            .limit(limit)
            .all()
        )

        scheduled = 0
        for row in due:
            send_campaign_step.delay(campaign_id=row.campaign_id)
            row.executed = True
            scheduled += 1

        db.commit()
        return {"processed": scheduled}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

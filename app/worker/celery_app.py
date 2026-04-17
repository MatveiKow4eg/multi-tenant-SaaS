from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "bot_lertisento",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.system",
        "app.tasks.finder",
        "app.tasks.researcher",
        "app.tasks.pipeline",
        "app.tasks.outreach",
        "app.tasks.mail_operator",
        "app.tasks.replies",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "mail-process-due-every-30-min": {
            "task": "mail.process_due_schedules",
            "schedule": 1800.0,
        },
        "reply-ingest-every-10-min": {
            "task": "reply.ingest_and_classify",
            "schedule": 600.0,
        },
    },
)


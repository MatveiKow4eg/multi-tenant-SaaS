from app.worker.celery_app import celery_app


@celery_app.task(name="system.ping")
def ping() -> str:
    return "pong"

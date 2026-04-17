from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.task import Task
from app.schemas.task import TaskCreate, TaskRead
from app.worker.celery_app import celery_app

router = APIRouter()


@router.post("/", response_model=TaskRead)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)) -> Task:
    task = Task(payload=payload.payload, status="queued")
    db.add(task)
    db.commit()
    db.refresh(task)

    celery_app.send_task("system.ping")
    return task


@router.get("/", response_model=list[TaskRead])
def list_tasks(db: Session = Depends(get_db)) -> list[Task]:
    return db.query(Task).order_by(Task.created_at.desc()).limit(100).all()

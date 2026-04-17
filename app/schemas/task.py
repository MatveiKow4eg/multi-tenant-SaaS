from datetime import datetime

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    payload: dict = Field(default_factory=dict)


class TaskRead(BaseModel):
    id: int
    status: str
    payload: dict
    created_at: datetime

    class Config:
        from_attributes = True

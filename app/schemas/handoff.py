from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class HandoffRead(BaseModel):
    id: int
    campaign_id: int
    company_id: int | None
    contact_id: int | None
    label: str
    priority: str
    needs_human: bool
    summary: str | None
    recommended_reply: str | None
    payload: dict | None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class HandoffStatusUpdate(BaseModel):
    status: Literal["new", "in_review", "done"]

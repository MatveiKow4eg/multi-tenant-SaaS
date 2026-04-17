from pydantic import BaseModel


class KpiOverview(BaseModel):
    companies_found: int
    companies_qualified: int
    contacts_found: int
    emails_sent: int
    replies_received: int
    warm_replies: int


class FunnelOverview(BaseModel):
    period: str
    period_start: str
    emails_sent: int
    replies_received: int
    warm_replies: int
    reply_rate: float
    positive_reply_rate: float


class StageCounts(BaseModel):
    found: int
    qualified: int
    contacts: int
    sent: int
    replies: int
    warm: int


class StageConversionRates(BaseModel):
    found_to_qualified: float
    qualified_to_contacts: float
    contacts_to_sent: float
    sent_to_replies: float
    replies_to_warm: float


class StageFunnelOverview(BaseModel):
    period: str
    period_start: str
    counts: StageCounts
    conversions: StageConversionRates

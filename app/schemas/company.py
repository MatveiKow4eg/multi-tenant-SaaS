from pydantic import BaseModel, Field


class FinderRunRequest(BaseModel):
    countries: list[str] = Field(min_length=1, examples=[["Lithuania", "Latvia"]])
    keywords: list[str] | None = None
    results_per_query: int = Field(default=10, ge=1, le=50)


class FinderRunResponse(BaseModel):
    task_id: str


class SearchPlannerRunRequest(BaseModel):
    intent: str = Field(
        min_length=10,
        examples=[
            "Ищи литовские заводы в сферах металлообработки, сварки, CNC и subcontract manufacturing."
        ],
    )
    country: str = Field(default="Lithuania")
    results_per_query: int = Field(default=20, ge=1, le=50)


class SearchPlannerRunResponse(BaseModel):
    task_id: str
    plan_summary: dict


class ResearchRunResponse(BaseModel):
    task_id: str


class OutreachRunResponse(BaseModel):
    task_id: str


class ResearchBatchRequest(BaseModel):
    company_ids: list[int] = Field(min_length=1)


class CompanyRead(BaseModel):
    id: int
    domain: str
    name: str | None
    country: str | None
    industry: str | None
    score: float | None
    status: str

    class Config:
        from_attributes = True


class CompanyDetailRead(CompanyRead):
    qualification_result: dict | None


class ContactRead(BaseModel):
    id: int
    email: str
    full_name: str | None
    role: str | None
    source_url: str | None
    confidence: float | None

    class Config:
        from_attributes = True

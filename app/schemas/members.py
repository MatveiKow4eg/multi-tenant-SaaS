from pydantic import BaseModel, Field


class TenantMemberRead(BaseModel):
    id: int
    user_id: int
    email: str
    full_name: str | None
    role: str
    status: str


class TenantMemberCreateRequest(BaseModel):
    email: str
    role: str = Field(default="operator")
    full_name: str | None = None
    password: str | None = Field(default=None, min_length=8)


class TenantMemberUpdateRequest(BaseModel):
    role: str | None = None
    status: str | None = None


class TenantInviteCreateRequest(BaseModel):
    email: str
    role: str = Field(default="operator")
    expires_in_hours: int = Field(default=72, ge=1, le=336)


class TenantInviteRead(BaseModel):
    id: int
    tenant_id: int
    email: str
    role: str
    status: str
    expires_at: str
    invite_token: str

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    full_name: str | None = None
    tenant_name: str | None = Field(default=None, min_length=2, max_length=255)


class LoginRequest(BaseModel):
    email: str
    password: str
    tenant_slug: str | None = None


class AcceptInviteRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)
    full_name: str | None = None


class ResendVerificationRequest(BaseModel):
    email: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)


class TenantMembershipRead(BaseModel):
    tenant_id: int
    tenant_slug: str
    tenant_name: str
    role: str


class AuthSessionRead(BaseModel):
    token: str
    tenant_id: int
    tenant_slug: str
    user_id: int


class MeResponse(BaseModel):
    user_id: int
    email: str
    full_name: str | None
    memberships: list[TenantMembershipRead]

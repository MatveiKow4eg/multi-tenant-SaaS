from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="bot-lertisento", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    app_debug: bool = Field(default=True, alias="APP_DEBUG")
    app_auto_create_tables: bool = Field(default=False, alias="APP_AUTO_CREATE_TABLES")
    auth_enforce_rbac: bool = Field(default=False, alias="AUTH_ENFORCE_RBAC")
    auth_require_email_verified: bool = Field(default=False, alias="AUTH_REQUIRE_EMAIL_VERIFIED")
    app_public_base_url: str = Field(default="http://localhost:8000", alias="APP_PUBLIC_BASE_URL")
    auth_session_cookie_name: str = Field(default="auth_session", alias="AUTH_SESSION_COOKIE_NAME")
    auth_session_cookie_secure: bool = Field(default=False, alias="AUTH_SESSION_COOKIE_SECURE")
    auth_session_rotate_before_hours: int = Field(default=24, alias="AUTH_SESSION_ROTATE_BEFORE_HOURS")
    auth_resend_verification_cooldown_seconds: int = Field(
        default=300,
        alias="AUTH_RESEND_VERIFICATION_COOLDOWN_SECONDS",
    )
    auth_forgot_password_cooldown_seconds: int = Field(
        default=300,
        alias="AUTH_FORGOT_PASSWORD_COOLDOWN_SECONDS",
    )
    auth_login_max_attempts: int = Field(default=5, alias="AUTH_LOGIN_MAX_ATTEMPTS")
    auth_login_attempt_window_seconds: int = Field(
        default=300,
        alias="AUTH_LOGIN_ATTEMPT_WINDOW_SECONDS",
    )
    auth_login_lockout_seconds: int = Field(default=900, alias="AUTH_LOGIN_LOCKOUT_SECONDS")
    auth_csrf_cookie_name: str = Field(default="csrf_token", alias="AUTH_CSRF_COOKIE_NAME")
    auth_csrf_cookie_secure: bool = Field(default=False, alias="AUTH_CSRF_COOKIE_SECURE")

    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")

    zone_smtp_host: str = Field(default="smtp.zone.eu", alias="ZONE_SMTP_HOST")
    zone_smtp_port: int = Field(default=587, alias="ZONE_SMTP_PORT")
    zone_imap_host: str = Field(default="imap.zone.eu", alias="ZONE_IMAP_HOST")
    zone_imap_port: int = Field(default=993, alias="ZONE_IMAP_PORT")
    zone_email: str = Field(alias="ZONE_EMAIL")
    zone_password: str = Field(alias="ZONE_PASSWORD")
    zone_use_starttls: bool = Field(default=True, alias="ZONE_USE_STARTTLS")
    mail_daily_limit: int = Field(default=100, alias="MAIL_DAILY_LIMIT")
    mail_min_interval_seconds: int = Field(default=30, alias="MAIL_MIN_INTERVAL_SECONDS")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_main_model: str = Field(default="gpt-5.4", alias="OPENAI_MAIN_MODEL")
    openai_mini_model: str = Field(default="gpt-5.4-mini", alias="OPENAI_MINI_MODEL")

    search_provider: str = Field(default="api", alias="SEARCH_PROVIDER")
    search_api_key: str | None = Field(default=None, alias="SEARCH_API_KEY")
    search_api_url: str = Field(default="https://google.serper.dev/search", alias="SEARCH_API_URL")
    search_provider_fallback_to_ddg: bool = Field(default=False, alias="SEARCH_PROVIDER_FALLBACK_TO_DDG")

    outreach_allowed_countries: str = Field(default="Lithuania", alias="OUTREACH_ALLOWED_COUNTRIES")


settings = Settings()

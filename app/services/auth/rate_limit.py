from __future__ import annotations

import logging

from redis import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)


def _mask_email(email: str) -> str:
    normalized = email.strip().lower()
    if "@" not in normalized:
        return "***"
    local, domain = normalized.split("@", 1)
    if len(local) <= 2:
        local_masked = "*" * len(local)
    else:
        local_masked = local[:1] + ("*" * (len(local) - 2)) + local[-1:]
    return f"{local_masked}@{domain}"


def acquire_email_cooldown(*, purpose: str, email: str, ttl_seconds: int) -> bool:
    """Return True if action is allowed now, False if cooldown is active.

    Fail-open behavior: if Redis is unavailable, returns True.
    """
    key = f"auth:{purpose}:{email.strip().lower()}"
    try:
        redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        allowed = bool(redis_client.set(key, "1", ex=ttl_seconds, nx=True))
        if not allowed:
            logger.info(
                "Auth cooldown active: purpose=%s email=%s ttl=%s",
                purpose,
                _mask_email(email),
                ttl_seconds,
            )
        return allowed
    except Exception as exc:
        logger.warning(
            "Auth cooldown fail-open: purpose=%s email=%s reason=%s",
            purpose,
            _mask_email(email),
            exc.__class__.__name__,
        )
        return True


def _login_attempt_key(email: str) -> str:
    return f"auth:login-attempts:{email.strip().lower()}"


def _login_lock_key(email: str) -> str:
    return f"auth:login-lock:{email.strip().lower()}"


def is_login_allowed(*, email: str) -> bool:
    try:
        redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        locked = bool(redis_client.exists(_login_lock_key(email)))
        if locked:
            logger.info("Auth login lock active: email=%s", _mask_email(email))
        return not locked
    except Exception as exc:
        logger.warning(
            "Auth login allow-check fail-open: email=%s reason=%s",
            _mask_email(email),
            exc.__class__.__name__,
        )
        return True


def register_login_failure(
    *,
    email: str,
    max_attempts: int,
    window_seconds: int,
    lockout_seconds: int,
) -> bool:
    """Record a failed login. Returns True if lockout became active on this failure."""
    try:
        redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        attempts_key = _login_attempt_key(email)
        attempts = int(redis_client.incr(attempts_key))
        if attempts == 1:
            redis_client.expire(attempts_key, window_seconds)
        if attempts >= max_attempts:
            redis_client.set(_login_lock_key(email), "1", ex=lockout_seconds)
            redis_client.delete(attempts_key)
            logger.warning(
                "Auth login lock triggered: email=%s attempts=%s lockout=%s",
                _mask_email(email),
                attempts,
                lockout_seconds,
            )
            return True
        logger.info(
            "Auth login failure tracked: email=%s attempts=%s window=%s",
            _mask_email(email),
            attempts,
            window_seconds,
        )
        return False
    except Exception as exc:
        logger.warning(
            "Auth login failure tracking fail-open: email=%s reason=%s",
            _mask_email(email),
            exc.__class__.__name__,
        )
        return False


def clear_login_failures(*, email: str) -> None:
    try:
        redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        redis_client.delete(_login_attempt_key(email), _login_lock_key(email))
    except Exception as exc:
        logger.warning(
            "Auth login failure reset skipped: email=%s reason=%s",
            _mask_email(email),
            exc.__class__.__name__,
        )

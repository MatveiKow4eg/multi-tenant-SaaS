from app.services.auth import rate_limit


class _FakeRedisAllowAlways:
    def set(self, key: str, value: str, ex: int, nx: bool):
        return True


class _FakeRedisCooldown:
    def __init__(self):
        self._seen: set[str] = set()

    def set(self, key: str, value: str, ex: int, nx: bool):
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


class _FakeRedisLogin:
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.keys: set[str] = set()

    def exists(self, key: str):
        return 1 if key in self.keys else 0

    def incr(self, key: str):
        value = self.counters.get(key, 0) + 1
        self.counters[key] = value
        return value

    def expire(self, key: str, seconds: int):
        return True

    def set(self, key: str, value: str, ex: int, nx: bool = False):
        if nx and key in self.keys:
            return False
        self.keys.add(key)
        return True

    def delete(self, *keys: str):
        for key in keys:
            self.keys.discard(key)
            self.counters.pop(key, None)
        return len(keys)


def test_acquire_email_cooldown_allows_when_key_new(monkeypatch):
    monkeypatch.setattr(rate_limit.Redis, "from_url", lambda *args, **kwargs: _FakeRedisAllowAlways())

    ok = rate_limit.acquire_email_cooldown(
        purpose="forgot-password",
        email="user@example.com",
        ttl_seconds=60,
    )

    assert ok is True


def test_acquire_email_cooldown_blocks_when_key_exists(monkeypatch):
    fake = _FakeRedisCooldown()
    monkeypatch.setattr(rate_limit.Redis, "from_url", lambda *args, **kwargs: fake)

    first = rate_limit.acquire_email_cooldown(
        purpose="resend-verify",
        email="user@example.com",
        ttl_seconds=60,
    )
    second = rate_limit.acquire_email_cooldown(
        purpose="resend-verify",
        email="user@example.com",
        ttl_seconds=60,
    )

    assert first is True
    assert second is False


def test_acquire_email_cooldown_fail_open_when_redis_unavailable(monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("redis-down")

    monkeypatch.setattr(rate_limit.Redis, "from_url", _raise)

    ok = rate_limit.acquire_email_cooldown(
        purpose="forgot-password",
        email="user@example.com",
        ttl_seconds=60,
    )

    assert ok is True


def test_login_rate_limit_locks_after_threshold(monkeypatch):
    fake = _FakeRedisLogin()
    monkeypatch.setattr(rate_limit.Redis, "from_url", lambda *args, **kwargs: fake)

    assert rate_limit.is_login_allowed(email="user@example.com") is True
    assert rate_limit.register_login_failure(
        email="user@example.com",
        max_attempts=3,
        window_seconds=60,
        lockout_seconds=120,
    ) is False
    assert rate_limit.register_login_failure(
        email="user@example.com",
        max_attempts=3,
        window_seconds=60,
        lockout_seconds=120,
    ) is False
    assert rate_limit.register_login_failure(
        email="user@example.com",
        max_attempts=3,
        window_seconds=60,
        lockout_seconds=120,
    ) is True
    assert rate_limit.is_login_allowed(email="user@example.com") is False


def test_login_rate_limit_clear_resets_state(monkeypatch):
    fake = _FakeRedisLogin()
    monkeypatch.setattr(rate_limit.Redis, "from_url", lambda *args, **kwargs: fake)

    rate_limit.register_login_failure(
        email="user@example.com",
        max_attempts=1,
        window_seconds=60,
        lockout_seconds=120,
    )
    assert rate_limit.is_login_allowed(email="user@example.com") is False

    rate_limit.clear_login_failures(email="user@example.com")

    assert rate_limit.is_login_allowed(email="user@example.com") is True

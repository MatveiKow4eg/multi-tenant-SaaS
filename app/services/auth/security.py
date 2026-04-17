from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
import hashlib
import hmac
import secrets


def _b64(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")

    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored_hash.split("$", 2)
    except ValueError:
        return False

    if scheme != "scrypt":
        return False

    try:
        pad_salt = "=" * ((4 - len(salt_b64) % 4) % 4)
        pad_digest = "=" * ((4 - len(digest_b64) % 4) % 4)
        salt = urlsafe_b64decode(salt_b64 + pad_salt)
        expected_digest = urlsafe_b64decode(digest_b64 + pad_digest)
    except Exception:
        return False

    actual_digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return hmac.compare_digest(actual_digest, expected_digest)


def generate_session_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

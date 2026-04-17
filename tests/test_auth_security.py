from app.services.auth.security import generate_session_token, hash_password, hash_token, verify_password


def test_password_hash_and_verify_roundtrip():
    raw = "S3cure-Password!"
    password_hash = hash_password(raw)

    assert password_hash.startswith("scrypt$")
    assert verify_password(raw, password_hash) is True
    assert verify_password("wrong-password", password_hash) is False


def test_session_token_hashing_is_stable():
    token = generate_session_token()

    digest_1 = hash_token(token)
    digest_2 = hash_token(token)

    assert token
    assert len(digest_1) == 64
    assert digest_1 == digest_2

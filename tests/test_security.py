"""Password hashing and JWT handling, without HTTP or a database."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import get_settings
from app.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_hash_is_salted_and_verifies() -> None:
    first, second = hash_password("s3cret-pass"), hash_password("s3cret-pass")
    assert first != second
    assert first.startswith("$argon2id$")
    assert verify_password("s3cret-pass", first)
    assert not verify_password("wrong-pass", first)


def test_verify_rejects_malformed_hash() -> None:
    assert not verify_password("anything", "not-a-hash")


def test_token_round_trip() -> None:
    assert decode_access_token(create_access_token(42)) == 42


def test_expired_token_rejected() -> None:
    token = create_access_token(42, expires_delta=timedelta(seconds=-1))
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_token_signed_with_other_secret_rejected() -> None:
    now = datetime.now(UTC)
    forged = jwt.encode(
        {"sub": "42", "iat": now, "exp": now + timedelta(minutes=5)},
        "some-other-secret-that-is-at-least-32-bytes",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(forged)


def test_unsigned_token_rejected() -> None:
    now = datetime.now(UTC)
    unsigned = jwt.encode(
        {"sub": "42", "iat": now, "exp": now + timedelta(minutes=5)}, None, algorithm="none"
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(unsigned)


def test_token_without_subject_rejected() -> None:
    now = datetime.now(UTC)
    token = jwt.encode(
        {"iat": now, "exp": now + timedelta(minutes=5)},
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)

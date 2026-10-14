from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings

_hasher = PasswordHasher()

# Verified against when the email doesn't exist, so a failed login takes the same time
# whether or not the account exists (prevents discovering registered emails by timing).
_DUMMY_HASH = _hasher.hash("not-a-real-password")


class InvalidTokenError(Exception):
    """The access token is missing a claim, malformed, expired or has a bad signature."""


def hash_password(password: str) -> str:
    """Hash a password with Argon2id (salt and parameters are embedded in the result)."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return True if the password matches the stored hash."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def burn_password_check(password: str) -> None:
    """Spend the same time as a real verification, for logins with an unknown email."""
    verify_password(password, _DUMMY_HASH)


def create_access_token(user_id: int, expires_delta: timedelta | None = None) -> str:
    """Issue a signed JWT whose subject is the user id."""
    settings = get_settings()
    now = datetime.now(UTC)
    lifetime = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    claims = {"sub": str(user_id), "iat": now, "exp": now + lifetime}
    return jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )


def decode_access_token(token: str) -> int:
    """Validate a token and return the user id it was issued for."""
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            # Pinning the algorithm list blocks "alg: none" and algorithm-confusion attacks.
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "iat"]},
        )
        return int(claims["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidTokenError(str(exc)) from exc

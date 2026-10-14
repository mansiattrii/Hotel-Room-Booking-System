import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import violated_constraint
from app.errors import ConflictError, UnauthorizedError
from app.models import User
from app.security import burn_password_check, hash_password, verify_password

logger = logging.getLogger(__name__)


def register_user(db: Session, email: str, password: str, role: str = "user") -> User:
    """Create a user. Raises ConflictError if the email is taken."""
    user = User(email=email, password_hash=hash_password(password), role=role)
    db.add(user)
    # No "SELECT first to see if the email exists": two concurrent registrations could both
    # pass that check. The unique constraint decides, and we translate its error.
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if violated_constraint(exc) == "uq_users_email":
            raise ConflictError("Email is already registered", code="email_taken") from exc
        raise
    logger.info("user registered", extra={"user_id": user.id, "role": user.role})
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    """Return the user for valid credentials, otherwise raise UnauthorizedError.

    The error is identical for an unknown email and a wrong password, so the response
    doesn't reveal which emails are registered.
    """
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        burn_password_check(password)
        raise UnauthorizedError("Invalid email or password", code="invalid_credentials")
    if not verify_password(password, user.password_hash):
        raise UnauthorizedError("Invalid email or password", code="invalid_credentials")
    return user


def promote_to_admin(db: Session, email: str) -> User | None:
    """Give an existing user the admin role. Returns None if no such user."""
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        return None
    user.role = "admin"
    db.commit()
    logger.info("user promoted to admin", extra={"user_id": user.id})
    return user

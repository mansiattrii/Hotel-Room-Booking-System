from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import ForbiddenError, UnauthorizedError
from app.models import User
from app.security import InvalidTokenError, decode_access_token

# auto_error=False so a missing header produces our own 401 body, not FastAPI's default.
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the bearer token to a user, or fail with 401."""
    if credentials is None:
        raise UnauthorizedError("Missing bearer token")
    try:
        user_id = decode_access_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Invalid or expired token") from exc

    # The role is read from the database, not the token, so revoking admin rights
    # takes effect on the next request rather than when the token expires.
    user = db.get(User, user_id)
    if user is None:
        raise UnauthorizedError("Invalid or expired token")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Allow only admins; authenticated non-admins get 403."""
    if user.role != "admin":
        raise ForbiddenError("Admin role required")
    return user

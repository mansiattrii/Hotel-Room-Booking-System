from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.schemas import ErrorResponse, LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.security import create_access_token
from app.services import users

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"model": ErrorResponse}},
)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> UserOut:
    """Create a user account with the `user` role."""
    user = users.register_user(db, body.email, body.password)
    return UserOut.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}},
)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Exchange email and password for a bearer access token."""
    user = users.authenticate(db, body.email, body.password)
    return TokenResponse(
        access_token=create_access_token(user.id),
        expires_in=get_settings().access_token_expire_minutes * 60,
    )

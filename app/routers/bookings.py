from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import BookingCreate, BookingOut, ConfirmRequest, ErrorResponse, Page
from app.services import booking as booking_service
from app.services.booking import BookingInput

router = APIRouter(
    prefix="/bookings",
    tags=["bookings"],
    responses={status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}},
)

_OWNERSHIP_ERRORS = {
    status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=BookingOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
def create_booking(
    body: BookingCreate,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookingOut:
    """Hold a room for HOLD_MINUTES. Retrying with the same Idempotency-Key is safe."""
    booking, replayed = booking_service.create_booking(
        db,
        user,
        BookingInput(
            room_id=body.room_id,
            check_in=body.check_in,
            check_out=body.check_out,
            guests=body.guests,
        ),
        idempotency_key,
    )
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return BookingOut.model_validate(booking)


@router.get("/me", response_model=Page[BookingOut])
def my_bookings(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Page[BookingOut]:
    """The current user's bookings, newest first."""
    bookings, total = booking_service.list_user_bookings(db, user.id, page, page_size)
    return Page[BookingOut](
        items=[BookingOut.model_validate(b) for b in bookings],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{booking_id}", response_model=BookingOut, responses=_OWNERSHIP_ERRORS)
def get_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookingOut:
    """One booking. Visible to its owner and to admins."""
    return BookingOut.model_validate(booking_service.get_booking(db, user, booking_id))


@router.post(
    "/{booking_id}/confirm",
    response_model=BookingOut,
    responses={
        **_OWNERSHIP_ERRORS,
        status.HTTP_402_PAYMENT_REQUIRED: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
def confirm_booking(
    booking_id: int,
    body: ConfirmRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookingOut:
    """Pay for a held booking (mock payment) and confirm it."""
    booking = booking_service.confirm_booking(db, user, booking_id, body.payment_token)
    return BookingOut.model_validate(booking)


@router.post(
    "/{booking_id}/cancel",
    response_model=BookingOut,
    responses={**_OWNERSHIP_ERRORS, status.HTTP_409_CONFLICT: {"model": ErrorResponse}},
)
def cancel_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookingOut:
    """Cancel a booking. Owner or admin only."""
    return BookingOut.model_validate(booking_service.cancel_booking(db, user, booking_id))

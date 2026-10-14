"""Booking lifecycle: hold, confirm, cancel, expire.

Transactions: every public function here is one transaction on the request's session,
ending in an explicit commit or rollback. Isolation level is READ COMMITTED (set on the
engine). Correctness under concurrency comes from row locks (SELECT ... FOR UPDATE on the
booking or room) and constraints, not from a stricter isolation level; NOTES.md explains why.
"""

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Integer, bindparam, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import violated_constraint
from app.errors import (
    ConflictError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    PaymentDeclinedError,
)
from app.models import Booking, Payment, Room, RoomType, User
from app.services import payments
from app.services.locking import HoldRequest, Strategy, place_hold

logger = logging.getLogger(__name__)

EXCLUSION_CONSTRAINT = "ex_bookings_no_overlap"
IDEMPOTENCY_CONSTRAINT = "uq_bookings_user_id_idempotency_key"


@dataclass(frozen=True)
class BookingInput:
    room_id: int
    check_in: date
    check_out: date
    guests: int


def create_booking(
    db: Session,
    user: User,
    data: BookingInput,
    idempotency_key: str,
    strategy: Strategy | None = None,
) -> tuple[Booking, bool]:
    """Place a hold on a room. Returns (booking, replayed).

    `replayed` is True when the idempotency key was already used for this exact request,
    in which case the original booking is returned and nothing new is created.
    """
    settings = get_settings()
    strategy = strategy or Strategy(settings.booking_strategy)

    existing = _find_by_key(db, user.id, idempotency_key)
    if existing is not None:
        return _replay(existing, data), True

    room = db.execute(
        select(RoomType.capacity, RoomType.base_price_per_night)
        .join(Room, Room.room_type_id == RoomType.id)
        .where(Room.id == data.room_id)
    ).one_or_none()
    if room is None:
        raise NotFoundError(f"Room {data.room_id} not found")
    if data.guests > room.capacity:
        raise InvalidInputError(
            f"Room {data.room_id} holds at most {room.capacity} guests",
            code="guests_exceed_capacity",
        )

    nights = (data.check_out - data.check_in).days
    request = HoldRequest(
        user_id=user.id,
        room_id=data.room_id,
        check_in=data.check_in,
        check_out=data.check_out,
        guests=data.guests,
        total_price=room.base_price_per_night * nights,
        idempotency_key=idempotency_key,
        hold_minutes=settings.hold_minutes,
    )
    try:
        booking = place_hold(db, strategy, request)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        constraint = violated_constraint(exc)
        if constraint == EXCLUSION_CONSTRAINT:
            raise ConflictError(
                "Room is not available for these dates", code="room_unavailable"
            ) from exc
        if constraint == IDEMPOTENCY_CONSTRAINT:
            # A concurrent request with the same key committed first.
            winner = _find_by_key(db, user.id, idempotency_key)
            if winner is not None:
                return _replay(winner, data), True
        raise
    except Exception:
        db.rollback()
        raise

    logger.info(
        "booking held",
        extra={"booking_id": booking.id, "room_id": booking.room_id, "strategy": strategy},
    )
    return booking, False


def _find_by_key(db: Session, user_id: int, key: str) -> Booking | None:
    return db.scalar(
        select(Booking).where(Booking.user_id == user_id, Booking.idempotency_key == key)
    )


def _replay(existing: Booking, data: BookingInput) -> Booking:
    same_request = (
        existing.room_id == data.room_id
        and existing.check_in == data.check_in
        and existing.check_out == data.check_out
        and existing.guests == data.guests
    )
    if not same_request:
        raise InvalidInputError(
            "Idempotency-Key was already used with a different request body",
            code="idempotency_key_reused",
        )
    return existing


def _lock_booking(db: Session, user: User, booking_id: int) -> tuple[Booking, bool]:
    """Lock a booking row for the rest of the transaction. Returns (booking, hold_lapsed).

    The lock serialises confirm, cancel and the expiry worker for the same booking.
    populate_existing makes sure we use the row as it is *after* acquiring the lock,
    not a copy the session loaded earlier.
    """
    row = db.execute(
        select(Booking, Booking.hold_expires_at <= func.now())
        .where(Booking.id == booking_id)
        .with_for_update(of=Booking)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        db.rollback()
        raise NotFoundError(f"Booking {booking_id} not found")
    booking, hold_lapsed = row
    if booking.user_id != user.id and user.role != "admin":
        db.rollback()
        raise ForbiddenError("You can only access your own bookings")
    return booking, bool(hold_lapsed)


def confirm_booking(db: Session, user: User, booking_id: int, payment_token: str) -> Booking:
    """Charge for a held booking and confirm it, in one transaction.

    Raises ConflictError if the booking is cancelled, expired or its hold has lapsed,
    and PaymentDeclinedError (after recording the failed attempt) if the charge fails.
    Confirming an already confirmed booking returns it without charging again.
    """
    booking, hold_lapsed = _lock_booking(db, user, booking_id)

    if booking.status == "confirmed":
        db.commit()
        return booking
    if booking.status in ("cancelled", "expired"):
        db.rollback()
        raise ConflictError(f"Booking is {booking.status}", code=f"booking_{booking.status}")
    if hold_lapsed:
        booking.status = "expired"
        db.commit()
        raise ConflictError("The hold on this booking has expired", code="hold_expired")

    # A real provider call would not be made while holding a row lock; see NOTES.md.
    result = payments.charge(booking.total_price, payment_token)
    db.add(
        Payment(
            booking_id=booking.id,
            amount=booking.total_price,
            status="succeeded" if result.succeeded else "failed",
            provider_ref=result.provider_ref,
        )
    )
    if not result.succeeded:
        db.commit()
        logger.info("payment declined", extra={"booking_id": booking.id})
        raise PaymentDeclinedError("Payment was declined; the hold is still active")

    booking.status = "confirmed"
    db.commit()
    logger.info("booking confirmed", extra={"booking_id": booking.id})
    return booking


def cancel_booking(db: Session, user: User, booking_id: int) -> Booking:
    """Cancel a held or confirmed booking. Cancelling twice is a no-op."""
    booking, _ = _lock_booking(db, user, booking_id)
    if booking.status == "expired":
        db.rollback()
        raise ConflictError("Booking has already expired", code="booking_expired")
    if booking.status != "cancelled":
        booking.status = "cancelled"
    db.commit()
    logger.info("booking cancelled", extra={"booking_id": booking.id})
    return booking


def get_booking(db: Session, user: User, booking_id: int) -> Booking:
    """A booking visible to its owner and to admins."""
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise NotFoundError(f"Booking {booking_id} not found")
    if booking.user_id != user.id and user.role != "admin":
        raise ForbiddenError("You can only access your own bookings")
    return booking


def list_user_bookings(
    db: Session, user_id: int, page: int, page_size: int
) -> tuple[list[Booking], int]:
    """A user's bookings, newest first."""
    total = db.scalar(select(func.count()).where(Booking.user_id == user_id)) or 0
    bookings = db.scalars(
        select(Booking)
        .where(Booking.user_id == user_id)
        .order_by(Booking.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    return list(bookings), total


EXPIRE_LAPSED_HOLDS = text(
    """
    UPDATE bookings
    SET status = 'expired', updated_at = now()
    WHERE id IN (
        SELECT id
        FROM bookings
        WHERE status = 'held' AND hold_expires_at <= now()
        ORDER BY hold_expires_at
        LIMIT :batch_size
        FOR UPDATE SKIP LOCKED
    )
    """
).bindparams(bindparam("batch_size", type_=Integer))


def expire_lapsed_holds(db: Session, batch_size: int = 1000) -> int:
    """Mark holds past their expiry as expired, freeing their rooms. Returns the count.

    SKIP LOCKED leaves alone any hold that a confirm or cancel currently has locked:
    that transaction will decide its fate, and several workers can run side by side.
    """
    expired_total = 0
    while True:
        expired = db.execute(EXPIRE_LAPSED_HOLDS, {"batch_size": batch_size}).rowcount
        db.commit()
        expired_total += expired
        if expired < batch_size:
            return expired_total

"""Three ways to stop two transactions booking the same room for overlapping dates.

All three run inside the caller's transaction at READ COMMITTED (set on the engine).

naive        SELECT for overlaps, then INSERT. Two transactions can both pass the SELECT
             before either inserts, so this double-books under concurrency. It exists only
             to demonstrate that race and must never be the configured strategy.
pessimistic  Lock the room row with SELECT ... FOR UPDATE first. A second transaction for
             the same room waits at the lock until the first commits; its overlap SELECT
             then runs with a fresh READ COMMITTED snapshot and sees the new booking.
constraint   INSERT directly. The exclusion constraint ex_bookings_no_overlap rejects an
             overlapping row atomically; the caller maps that error to 409. (Default.)

Every strategy first expires stale holds that would block the requested dates, so a
lapsed hold never causes a false 409 just because the worker hasn't run yet.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, Date, bindparam, func, insert, text
from sqlalchemy.orm import Session

from app.errors import ConflictError
from app.models import Booking


class Strategy(StrEnum):
    NAIVE = "naive"
    PESSIMISTIC = "pessimistic"
    CONSTRAINT = "constraint"


@dataclass(frozen=True)
class HoldRequest:
    user_id: int
    room_id: int
    check_in: date
    check_out: date
    guests: int
    total_price: Decimal
    idempotency_key: str
    hold_minutes: int


# Ids must be bound as BIGINT to match the columns. btree_gist only indexes bigint = bigint,
# so an INTEGER parameter silently stops the exclusion-constraint index filtering by room.
_RANGE_PARAMS = (
    bindparam("room_id", type_=BigInteger),
    bindparam("check_in", type_=Date),
    bindparam("check_out", type_=Date),
)

_LOCK_ROOM = text("SELECT id FROM rooms WHERE id = :room_id FOR UPDATE").bindparams(
    bindparam("room_id", type_=BigInteger)
)

_EXPIRE_STALE_HOLDS = text(
    """
    UPDATE bookings
    SET status = 'expired', updated_at = now()
    WHERE room_id = :room_id
      AND status = 'held'
      AND hold_expires_at <= now()
      AND daterange(check_in, check_out, '[)') && daterange(:check_in, :check_out, '[)')
    """
).bindparams(*_RANGE_PARAMS)

_HAS_OVERLAP = text(
    """
    SELECT EXISTS (
        SELECT 1
        FROM bookings
        WHERE room_id = :room_id
          AND status IN ('held', 'confirmed')
          AND daterange(check_in, check_out, '[)') && daterange(:check_in, :check_out, '[)')
    )
    """
).bindparams(*_RANGE_PARAMS)


def lock_room(db: Session, room_id: int) -> None:
    """Take a row lock on the room, held until the transaction ends."""
    db.execute(_LOCK_ROOM, {"room_id": room_id})


def expire_stale_holds(db: Session, room_id: int, check_in: date, check_out: date) -> int:
    """Mark lapsed holds that overlap the range as expired. Returns how many."""
    params = {"room_id": room_id, "check_in": check_in, "check_out": check_out}
    return db.execute(_EXPIRE_STALE_HOLDS, params).rowcount


def has_overlap(db: Session, room_id: int, check_in: date, check_out: date) -> bool:
    """True if an active booking of the room overlaps [check_in, check_out)."""
    params = {"room_id": room_id, "check_in": check_in, "check_out": check_out}
    return bool(db.scalar(_HAS_OVERLAP, params))


def insert_hold(db: Session, req: HoldRequest) -> Booking:
    """Insert a held booking. Expiry uses the database clock, like every other time check."""
    stmt = (
        insert(Booking)
        .values(
            user_id=req.user_id,
            room_id=req.room_id,
            check_in=req.check_in,
            check_out=req.check_out,
            guests=req.guests,
            status="held",
            hold_expires_at=func.now() + timedelta(minutes=req.hold_minutes),
            total_price=req.total_price,
            idempotency_key=req.idempotency_key,
        )
        .returning(Booking)
    )
    return db.scalars(stmt).one()


def _room_unavailable() -> ConflictError:
    return ConflictError("Room is not available for these dates", code="room_unavailable")


def _naive(db: Session, req: HoldRequest) -> Booking:
    expire_stale_holds(db, req.room_id, req.check_in, req.check_out)
    if has_overlap(db, req.room_id, req.check_in, req.check_out):
        raise _room_unavailable()
    return insert_hold(db, req)


def _pessimistic(db: Session, req: HoldRequest) -> Booking:
    lock_room(db, req.room_id)
    expire_stale_holds(db, req.room_id, req.check_in, req.check_out)
    if has_overlap(db, req.room_id, req.check_in, req.check_out):
        raise _room_unavailable()
    return insert_hold(db, req)


def _constraint(db: Session, req: HoldRequest) -> Booking:
    expire_stale_holds(db, req.room_id, req.check_in, req.check_out)
    return insert_hold(db, req)


_STRATEGIES: dict[Strategy, Callable[[Session, HoldRequest], Booking]] = {
    Strategy.NAIVE: _naive,
    Strategy.PESSIMISTIC: _pessimistic,
    Strategy.CONSTRAINT: _constraint,
}


def place_hold(db: Session, strategy: Strategy, req: HoldRequest) -> Booking:
    """Run the chosen strategy. Does not commit; the caller owns the transaction."""
    return _STRATEGIES[strategy](db, req)

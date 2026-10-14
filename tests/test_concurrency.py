"""Concurrency behaviour of the booking strategies, against real concurrent transactions.

Unlike the other tests, these commit data (separate connections must see it) and clean up
with TRUNCATE. The exclusion constraint is dropped where a test needs to judge a strategy
on its own, and restored afterwards.
"""

import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.errors import ConflictError
from app.models import User
from app.services import booking
from app.services.booking import BookingInput
from app.services.locking import (
    HoldRequest,
    Strategy,
    has_overlap,
    insert_hold,
    lock_room,
    place_hold,
)
from scripts import exclusion_toggle
from tests.conftest import day

RACERS = 25
CHECK_IN, CHECK_OUT = day(20), day(23)


@dataclass(frozen=True)
class SharedRoom:
    room_id: int
    user_ids: list[int]


@pytest.fixture
def race_engine(test_database_url: str) -> Iterator[Engine]:
    """An engine with a connection for every racer, so none waits on the pool."""
    engine = create_engine(
        test_database_url, pool_size=RACERS + 2, max_overflow=0, isolation_level="READ COMMITTED"
    )
    yield engine
    engine.dispose()


@pytest.fixture
def shared_room(engine: Engine) -> Iterator[SharedRoom]:
    with engine.begin() as conn:
        hotel_id = conn.scalar(
            text(
                "INSERT INTO hotels (name, city, address, star_rating) "
                "VALUES ('Race Hotel', 'Pune', 'Addr', 3) RETURNING id"
            )
        )
        room_type_id = conn.scalar(
            text(
                "INSERT INTO room_types (hotel_id, name, capacity, base_price_per_night) "
                "VALUES (:h, 'Standard', 2, 2000) RETURNING id"
            ),
            {"h": hotel_id},
        )
        room_id = conn.scalar(
            text(
                "INSERT INTO rooms (hotel_id, room_type_id, room_number) "
                "VALUES (:h, :rt, '101') RETURNING id"
            ),
            {"h": hotel_id, "rt": room_type_id},
        )
        user_ids = conn.scalars(
            text(
                "INSERT INTO users (email, password_hash) "
                "SELECT 'racer' || g || '@example.com', 'x' FROM generate_series(1, :n) g "
                "RETURNING id"
            ),
            {"n": RACERS},
        ).all()
    yield SharedRoom(room_id=room_id, user_ids=list(user_ids))
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE payments, bookings, rooms, room_types, hotels, users "
                "RESTART IDENTITY CASCADE"
            )
        )
        exclusion_toggle.enable(conn)


@pytest.fixture
def constraint_off(engine: Engine, shared_room: SharedRoom) -> Iterator[None]:
    with engine.begin() as conn:
        exclusion_toggle.disable(conn)
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE payments, bookings"))
        exclusion_toggle.enable(conn)


def _overlapping_pairs(engine: Engine) -> int:
    with engine.connect() as conn:
        return exclusion_toggle.count_overlapping_active_pairs(conn)


def _hold_request(room_id: int, user_id: int) -> HoldRequest:
    return HoldRequest(
        user_id=user_id,
        room_id=room_id,
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        guests=1,
        total_price=Decimal("6000.00"),
        idempotency_key=f"manual-{user_id}",
        hold_minutes=10,
    )


def _race(engine: Engine, strategy: Strategy, room: SharedRoom) -> Counter[str]:
    """Every user tries to book the same room for the same dates at the same moment."""
    start_line = Barrier(len(room.user_ids))

    def attempt(user_id: int) -> str:
        with Session(engine) as db:
            db.connection()  # check out a connection before the start line
            start_line.wait()
            try:
                booking.create_booking(
                    db,
                    User(id=user_id, role="user"),
                    BookingInput(room.room_id, CHECK_IN, CHECK_OUT, guests=1),
                    idempotency_key=f"race-{user_id}",
                    strategy=strategy,
                )
                return "booked"
            except ConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=len(room.user_ids)) as pool:
        return Counter(pool.map(attempt, room.user_ids))


def test_constraint_strategy_allows_exactly_one(
    race_engine: Engine, engine: Engine, shared_room: SharedRoom
) -> None:
    results = _race(race_engine, Strategy.CONSTRAINT, shared_room)
    assert results == Counter(booked=1, conflict=RACERS - 1)
    assert _overlapping_pairs(engine) == 0


def test_pessimistic_strategy_allows_exactly_one_without_constraint(
    race_engine: Engine, engine: Engine, shared_room: SharedRoom, constraint_off: None
) -> None:
    results = _race(race_engine, Strategy.PESSIMISTIC, shared_room)
    assert results == Counter(booked=1, conflict=RACERS - 1)
    assert _overlapping_pairs(engine) == 0


def test_naive_check_then_insert_double_books(
    engine: Engine, shared_room: SharedRoom, constraint_off: None
) -> None:
    """The race, played out step by step: both check, both see free, both insert."""
    first_user, second_user = shared_room.user_ids[:2]
    with Session(engine) as first, Session(engine) as second:
        assert not has_overlap(first, shared_room.room_id, CHECK_IN, CHECK_OUT)
        assert not has_overlap(second, shared_room.room_id, CHECK_IN, CHECK_OUT)
        insert_hold(first, _hold_request(shared_room.room_id, first_user))
        first.commit()
        insert_hold(second, _hold_request(shared_room.room_id, second_user))
        second.commit()

    assert _overlapping_pairs(engine) == 1


def test_exclusion_constraint_rejects_the_same_interleaving(
    engine: Engine, shared_room: SharedRoom
) -> None:
    first_user, second_user = shared_room.user_ids[:2]
    with Session(engine) as first, Session(engine) as second:
        assert not has_overlap(first, shared_room.room_id, CHECK_IN, CHECK_OUT)
        assert not has_overlap(second, shared_room.room_id, CHECK_IN, CHECK_OUT)
        insert_hold(first, _hold_request(shared_room.room_id, first_user))
        first.commit()
        with pytest.raises(IntegrityError) as exc_info:
            insert_hold(second, _hold_request(shared_room.room_id, second_user))
        assert exc_info.value.orig.sqlstate == "23P01"

    assert _overlapping_pairs(engine) == 0


def test_room_lock_makes_second_transaction_wait(engine: Engine, shared_room: SharedRoom) -> None:
    with Session(engine) as first, Session(engine) as second:
        lock_room(first, shared_room.room_id)
        second.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(OperationalError) as exc_info:
            lock_room(second, shared_room.room_id)
        assert exc_info.value.orig.sqlstate == "55P03"  # lock_not_available


def _wait_until_someone_waits_for_a_lock(engine: Engine, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE wait_event_type = 'Lock' AND datname = current_database()"
    )
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            if conn.scalar(query):
                return
        time.sleep(0.02)
    raise AssertionError("no transaction ended up waiting for the room lock")


@pytest.mark.parametrize(
    ("isolation_level", "expected_pairs"),
    [("READ COMMITTED", 0), ("REPEATABLE READ", 1)],
)
def test_pessimistic_lock_relies_on_read_committed(
    engine: Engine,
    shared_room: SharedRoom,
    constraint_off: None,
    isolation_level: str,
    expected_pairs: int,
) -> None:
    """Why the booking path runs at READ COMMITTED.

    The second transaction waits for the room lock. Under READ COMMITTED its next
    statement takes a fresh snapshot and sees the first booking. Under REPEATABLE READ it
    keeps the snapshot from *before* it waited, misses the booking, and double-books.
    """
    first_user, second_user = shared_room.user_ids[:2]
    second_engine = engine.execution_options(isolation_level=isolation_level)

    def second_attempt() -> str:
        with Session(second_engine) as db:
            try:
                place_hold(
                    db, Strategy.PESSIMISTIC, _hold_request(shared_room.room_id, second_user)
                )
                db.commit()
                return "booked"
            except ConflictError:
                db.rollback()
                return "conflict"

    with Session(engine) as first, ThreadPoolExecutor(max_workers=1) as pool:
        lock_room(first, shared_room.room_id)
        pending = pool.submit(second_attempt)
        _wait_until_someone_waits_for_a_lock(engine)
        insert_hold(first, _hold_request(shared_room.room_id, first_user))
        first.commit()
        outcome = pending.result(timeout=10)

    assert outcome == ("conflict" if expected_pairs == 0 else "booked")
    assert _overlapping_pairs(engine) == expected_pairs

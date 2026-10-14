"""Database-level constraints, exercised with raw SQL so no application code is involved."""

import itertools
from datetime import date, timedelta

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

EXCLUSION_VIOLATION = "23P01"
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"

BASE = date(2030, 1, 10)


def d(offset: int) -> date:
    return BASE + timedelta(days=offset)


def scalar(conn: Connection, sql: str, **params: object) -> int:
    return conn.execute(text(sql), params).scalar_one()


@pytest.fixture
def ids(connection: Connection) -> dict[str, int]:
    user = scalar(
        connection,
        "INSERT INTO users (email, password_hash) VALUES ('a@example.com', 'x') RETURNING id",
    )
    other_user = scalar(
        connection,
        "INSERT INTO users (email, password_hash) VALUES ('b@example.com', 'x') RETURNING id",
    )
    hotel = scalar(
        connection,
        "INSERT INTO hotels (name, city, address, star_rating) "
        "VALUES ('H', 'Pune', 'Addr', 4) RETURNING id",
    )
    room_type = scalar(
        connection,
        "INSERT INTO room_types (hotel_id, name, capacity, base_price_per_night) "
        "VALUES (:h, 'Deluxe', 2, 3500.00) RETURNING id",
        h=hotel,
    )
    room = scalar(
        connection,
        "INSERT INTO rooms (hotel_id, room_type_id, room_number) "
        "VALUES (:h, :rt, '101') RETURNING id",
        h=hotel,
        rt=room_type,
    )
    other_room = scalar(
        connection,
        "INSERT INTO rooms (hotel_id, room_type_id, room_number) "
        "VALUES (:h, :rt, '102') RETURNING id",
        h=hotel,
        rt=room_type,
    )
    return {
        "user": user,
        "other_user": other_user,
        "hotel": hotel,
        "room_type": room_type,
        "room": room,
        "other_room": other_room,
    }


_keys = itertools.count(1)


def insert_booking(
    conn: Connection,
    *,
    user_id: int,
    room_id: int,
    check_in: date,
    check_out: date,
    status: str = "confirmed",
    idempotency_key: str | None = None,
    with_hold_expiry: bool = True,
) -> int:
    """Insert a booking inside a savepoint so a failure leaves the test transaction usable."""
    with conn.begin_nested():
        return scalar(
            conn,
            "INSERT INTO bookings (user_id, room_id, check_in, check_out, guests, status, "
            "hold_expires_at, total_price, idempotency_key) "
            "VALUES (:u, :r, :ci, :co, 1, :s, "
            "CASE WHEN :exp THEN now() + interval '10 minutes' END, 100.00, :k) RETURNING id",
            u=user_id,
            r=room_id,
            ci=check_in,
            co=check_out,
            s=status,
            exp=with_hold_expiry,
            k=idempotency_key or f"key-{next(_keys)}",
        )


def assert_violates(sqlstate: str, exc_info: pytest.ExceptionInfo[IntegrityError]) -> None:
    assert exc_info.value.orig is not None
    assert exc_info.value.orig.sqlstate == sqlstate


# Existing booking occupies nights [d(0), d(5)), i.e. checks out on d(5).
@pytest.mark.parametrize(
    ("check_in", "check_out"),
    [
        pytest.param(d(-2), d(2), id="overlaps-start"),
        pytest.param(d(4), d(8), id="overlaps-end"),
        pytest.param(d(1), d(3), id="contained"),
        pytest.param(d(-1), d(6), id="contains"),
        pytest.param(d(0), d(5), id="identical"),
    ],
)
@pytest.mark.parametrize("existing_status", ["held", "confirmed"])
def test_overlapping_active_booking_rejected(
    connection: Connection,
    ids: dict[str, int],
    existing_status: str,
    check_in: date,
    check_out: date,
) -> None:
    insert_booking(
        connection,
        user_id=ids["user"],
        room_id=ids["room"],
        check_in=d(0),
        check_out=d(5),
        status=existing_status,
    )
    with pytest.raises(IntegrityError) as exc_info:
        insert_booking(
            connection,
            user_id=ids["other_user"],
            room_id=ids["room"],
            check_in=check_in,
            check_out=check_out,
            status="held",
        )
    assert_violates(EXCLUSION_VIOLATION, exc_info)


@pytest.mark.parametrize(
    ("check_in", "check_out"),
    [
        pytest.param(d(5), d(8), id="starts-on-checkout-day"),
        pytest.param(d(-3), d(0), id="ends-on-checkin-day"),
    ],
)
def test_back_to_back_bookings_allowed(
    connection: Connection, ids: dict[str, int], check_in: date, check_out: date
) -> None:
    insert_booking(
        connection, user_id=ids["user"], room_id=ids["room"], check_in=d(0), check_out=d(5)
    )
    insert_booking(
        connection,
        user_id=ids["other_user"],
        room_id=ids["room"],
        check_in=check_in,
        check_out=check_out,
    )


def test_same_dates_on_different_rooms_allowed(connection: Connection, ids: dict[str, int]) -> None:
    insert_booking(
        connection, user_id=ids["user"], room_id=ids["room"], check_in=d(0), check_out=d(5)
    )
    insert_booking(
        connection, user_id=ids["user"], room_id=ids["other_room"], check_in=d(0), check_out=d(5)
    )


@pytest.mark.parametrize("inactive_status", ["cancelled", "expired"])
def test_inactive_booking_does_not_block_room(
    connection: Connection, ids: dict[str, int], inactive_status: str
) -> None:
    insert_booking(
        connection,
        user_id=ids["user"],
        room_id=ids["room"],
        check_in=d(0),
        check_out=d(5),
        status=inactive_status,
    )
    insert_booking(
        connection, user_id=ids["other_user"], room_id=ids["room"], check_in=d(0), check_out=d(5)
    )


def test_reactivating_cancelled_booking_into_conflict_rejected(
    connection: Connection, ids: dict[str, int]
) -> None:
    cancelled = insert_booking(
        connection,
        user_id=ids["user"],
        room_id=ids["room"],
        check_in=d(0),
        check_out=d(5),
        status="cancelled",
    )
    insert_booking(
        connection, user_id=ids["other_user"], room_id=ids["room"], check_in=d(1), check_out=d(3)
    )
    with pytest.raises(IntegrityError) as exc_info, connection.begin_nested():
        connection.execute(
            text("UPDATE bookings SET status = 'confirmed' WHERE id = :id"), {"id": cancelled}
        )
    assert_violates(EXCLUSION_VIOLATION, exc_info)


@pytest.mark.parametrize(
    ("check_in", "check_out"),
    [pytest.param(d(0), d(0), id="zero-nights"), pytest.param(d(3), d(1), id="reversed")],
)
def test_check_out_must_be_after_check_in(
    connection: Connection, ids: dict[str, int], check_in: date, check_out: date
) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        insert_booking(
            connection,
            user_id=ids["user"],
            room_id=ids["room"],
            check_in=check_in,
            check_out=check_out,
        )
    assert_violates(CHECK_VIOLATION, exc_info)


def test_held_booking_requires_expiry(connection: Connection, ids: dict[str, int]) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        insert_booking(
            connection,
            user_id=ids["user"],
            room_id=ids["room"],
            check_in=d(0),
            check_out=d(2),
            status="held",
            with_hold_expiry=False,
        )
    assert_violates(CHECK_VIOLATION, exc_info)


def test_unknown_booking_status_rejected(connection: Connection, ids: dict[str, int]) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        insert_booking(
            connection,
            user_id=ids["user"],
            room_id=ids["room"],
            check_in=d(0),
            check_out=d(2),
            status="pending",
        )
    assert_violates(CHECK_VIOLATION, exc_info)


def test_idempotency_key_unique_per_user(connection: Connection, ids: dict[str, int]) -> None:
    insert_booking(
        connection,
        user_id=ids["user"],
        room_id=ids["room"],
        check_in=d(0),
        check_out=d(2),
        idempotency_key="same",
    )
    with pytest.raises(IntegrityError) as exc_info:
        insert_booking(
            connection,
            user_id=ids["user"],
            room_id=ids["room"],
            check_in=d(10),
            check_out=d(12),
            idempotency_key="same",
        )
    assert_violates(UNIQUE_VIOLATION, exc_info)

    insert_booking(
        connection,
        user_id=ids["other_user"],
        room_id=ids["room"],
        check_in=d(20),
        check_out=d(22),
        idempotency_key="same",
    )


def test_room_type_must_belong_to_same_hotel(connection: Connection, ids: dict[str, int]) -> None:
    other_hotel = scalar(
        connection,
        "INSERT INTO hotels (name, city, address, star_rating) "
        "VALUES ('H2', 'Goa', 'Addr', 3) RETURNING id",
    )
    with pytest.raises(IntegrityError) as exc_info, connection.begin_nested():
        connection.execute(
            text("INSERT INTO rooms (hotel_id, room_type_id, room_number) VALUES (:h, :rt, '201')"),
            {"h": other_hotel, "rt": ids["room_type"]},
        )
    assert_violates(FOREIGN_KEY_VIOLATION, exc_info)


def test_room_number_unique_within_hotel(connection: Connection, ids: dict[str, int]) -> None:
    with pytest.raises(IntegrityError) as exc_info, connection.begin_nested():
        connection.execute(
            text("INSERT INTO rooms (hotel_id, room_type_id, room_number) VALUES (:h, :rt, '101')"),
            {"h": ids["hotel"], "rt": ids["room_type"]},
        )
    assert_violates(UNIQUE_VIOLATION, exc_info)


def test_email_must_be_lowercase(connection: Connection) -> None:
    with pytest.raises(IntegrityError) as exc_info, connection.begin_nested():
        connection.execute(
            text("INSERT INTO users (email, password_hash) VALUES ('Mixed@Example.com', 'x')")
        )
    assert_violates(CHECK_VIOLATION, exc_info)


def test_many_failed_payments_but_only_one_success(
    connection: Connection, ids: dict[str, int]
) -> None:
    booking = insert_booking(
        connection, user_id=ids["user"], room_id=ids["room"], check_in=d(0), check_out=d(2)
    )
    insert_payment = text(
        "INSERT INTO payments (booking_id, amount, status, provider_ref) "
        "VALUES (:b, 100.00, :s, 'mock')"
    )
    for status in ("failed", "failed", "succeeded"):
        connection.execute(insert_payment, {"b": booking, "s": status})
    with pytest.raises(IntegrityError) as exc_info, connection.begin_nested():
        connection.execute(insert_payment, {"b": booking, "s": "succeeded"})
    assert_violates(UNIQUE_VIOLATION, exc_info)

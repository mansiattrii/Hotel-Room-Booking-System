from datetime import date

import pytest
from sqlalchemy import Connection, text

from scripts.seed import SeedConfig, seed

SMALL = SeedConfig(hotels=6, cities=3, rooms=60, bookings=900, users=25)
TODAY = date(2030, 6, 1)


def test_seed_produces_requested_volume(connection: Connection) -> None:
    counts = seed(connection, SMALL, TODAY)

    assert counts["users"] == 25
    assert counts["hotels"] == 6
    assert counts["rooms"] == 60
    assert counts["bookings"] == 900
    confirmed = connection.scalar(text("SELECT count(*) FROM bookings WHERE status='confirmed'"))
    assert counts["payments"] == confirmed
    assert connection.scalar(text("SELECT count(DISTINCT city) FROM hotels")) <= 3


def test_seeded_bookings_are_consistent(connection: Connection) -> None:
    seed(connection, SMALL, TODAY)

    # Overlaps are impossible (the exclusion constraint would have rejected the COPY);
    # these check the generator's own arithmetic.
    over_capacity = connection.scalar(
        text(
            "SELECT count(*) FROM bookings b "
            "JOIN rooms r ON r.id = b.room_id JOIN room_types rt ON rt.id = r.room_type_id "
            "WHERE b.guests > rt.capacity"
        )
    )
    wrong_price = connection.scalar(
        text(
            "SELECT count(*) FROM bookings b "
            "JOIN rooms r ON r.id = b.room_id JOIN room_types rt ON rt.id = r.room_type_id "
            "WHERE b.total_price <> rt.base_price_per_night * (b.check_out - b.check_in)"
        )
    )
    future = connection.scalar(
        text("SELECT count(*) FROM bookings WHERE check_in >= :t"), {"t": TODAY}
    )
    assert over_capacity == 0
    assert wrong_price == 0
    assert future > 0


def _fingerprint(connection: Connection) -> tuple[str, str]:
    """Hash of the generated content, ignoring surrogate ids (sequences don't roll back)."""
    hotels = connection.scalar(
        text(
            "SELECT md5(string_agg(concat_ws(':', name, city, star_rating), ',' ORDER BY id)) "
            "FROM hotels"
        )
    )
    bookings = connection.scalar(
        text(
            "SELECT md5(string_agg("
            "concat_ws(':', check_in, check_out, status, guests, total_price), ',' ORDER BY id)) "
            "FROM bookings"
        )
    )
    return hotels, bookings


def test_seed_is_deterministic(connection: Connection) -> None:
    fingerprints = []
    for _ in range(2):
        savepoint = connection.begin_nested()
        seed(connection, SMALL, TODAY)
        fingerprints.append(_fingerprint(connection))
        savepoint.rollback()
    assert fingerprints[0] == fingerprints[1]


def test_impossible_configuration_rejected() -> None:
    with pytest.raises(ValueError, match="do not fit"):
        SeedConfig(hotels=1, rooms=1, bookings=10_000).validate()

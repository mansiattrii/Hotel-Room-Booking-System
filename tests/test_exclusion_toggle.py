import pytest
from sqlalchemy import Connection, text

from app.config import get_settings
from scripts import exclusion_toggle

_DEFINITION = text(
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ex_bookings_no_overlap'"
)


def test_round_trip_restores_identical_constraint(connection: Connection) -> None:
    # DDL is transactional in PostgreSQL, so the test's rollback undoes all of this.
    original = connection.scalar(_DEFINITION)
    exclusion_toggle.disable(connection)
    assert not exclusion_toggle.is_enabled(connection)

    exclusion_toggle.enable(connection)
    assert connection.scalar(_DEFINITION) == original


class _FakeConnection:
    def __init__(self, database: str) -> None:
        self.database = database

    def scalar(self, *_: object) -> str:
        return self.database


def test_refuses_non_test_database() -> None:
    with pytest.raises(exclusion_toggle.UnsafeDatabaseError):
        exclusion_toggle.disable(_FakeConnection("hotel_booking"))  # type: ignore[arg-type]


def test_refuses_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "app_env", "production")
    with pytest.raises(exclusion_toggle.UnsafeDatabaseError):
        exclusion_toggle.disable(_FakeConnection("hotel_booking_test"))  # type: ignore[arg-type]

"""Each strategy's sequential behaviour (no concurrency) through the booking service."""

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import ConflictError
from app.models import User
from app.services import booking
from app.services.booking import BookingInput
from app.services.locking import Strategy
from tests.conftest import Catalog, day


@pytest.mark.parametrize("strategy", list(Strategy))
def test_every_strategy_rejects_overlap_sequentially(
    db_session: Session, catalog: Catalog, make_user: Callable[..., User], strategy: Strategy
) -> None:
    room = catalog.standard_rooms[0]
    booking.create_booking(
        db_session, make_user(), BookingInput(room, day(10), day(15), 1), "a", strategy
    )
    with pytest.raises(ConflictError):
        booking.create_booking(
            db_session, make_user(), BookingInput(room, day(12), day(13), 1), "b", strategy
        )


@pytest.mark.parametrize("strategy", list(Strategy))
def test_every_strategy_expires_a_lapsed_hold_first(
    db_session: Session, catalog: Catalog, make_user: Callable[..., User], strategy: Strategy
) -> None:
    room = catalog.standard_rooms[0]
    stale, _ = booking.create_booking(
        db_session, make_user(), BookingInput(room, day(10), day(15), 1), "a", strategy
    )
    db_session.execute(
        text("UPDATE bookings SET hold_expires_at = now() - interval '1 second' WHERE id = :id"),
        {"id": stale.id},
    )

    fresh, _ = booking.create_booking(
        db_session, make_user(), BookingInput(room, day(10), day(15), 1), "b", strategy
    )
    db_session.refresh(stale)
    assert stale.status == "expired"
    assert fresh.status == "held"


def test_default_strategy_is_constraint() -> None:
    from app.config import Settings

    assert Settings.model_fields["booking_strategy"].default == "constraint"

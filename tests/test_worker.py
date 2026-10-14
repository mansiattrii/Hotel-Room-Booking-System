import threading
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import worker
from app.models import User
from app.services.booking import expire_lapsed_holds
from tests.conftest import Catalog, day


def _insert(db: Session, user: User, room_id: int, offset: int, status: str, expiry: str) -> int:
    return db.scalar(
        text(
            "INSERT INTO bookings (user_id, room_id, check_in, check_out, guests, status, "
            f"hold_expires_at, total_price, idempotency_key) VALUES (:u, :r, :ci, :co, 1, :s, "
            f"{expiry}, 100, :k) RETURNING id"
        ),
        {
            "u": user.id,
            "r": room_id,
            "ci": day(offset),
            "co": day(offset + 2),
            "s": status,
            "k": f"w-{offset}",
        },
    )


@pytest.mark.parametrize("batch_size", [1000, 1])
def test_expires_only_lapsed_holds(
    db_session: Session,
    catalog: Catalog,
    make_user: Callable[..., User],
    batch_size: int,
) -> None:
    user, room = make_user(), catalog.standard_rooms[0]
    lapsed = [
        _insert(db_session, user, room, offset, "held", "now() - interval '1 minute'")
        for offset in (10, 20, 30)
    ]
    live = _insert(db_session, user, room, 40, "held", "now() + interval '5 minutes'")
    confirmed = _insert(db_session, user, room, 50, "confirmed", "now() - interval '1 hour'")

    assert expire_lapsed_holds(db_session, batch_size=batch_size) == 3

    statuses = dict(db_session.execute(text("SELECT id, status FROM bookings")).tuples().all())
    assert [statuses[i] for i in lapsed] == ["expired"] * 3
    assert statuses[live] == "held"
    assert statuses[confirmed] == "confirmed"


def test_worker_loop_stops_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()
    calls = 0

    def fake_run_once() -> int:
        nonlocal calls
        calls += 1
        if calls == 3:
            stop.set()
        return 0

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    worker.run_forever(stop, interval_seconds=0.01)
    assert calls == 3

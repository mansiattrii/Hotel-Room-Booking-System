from collections.abc import Callable
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Booking, User
from tests.conftest import STANDARD_PRICE, SUITE_PRICE, Catalog, day


def _availability(client: TestClient, hotel_id: int, check_in: date, check_out: date, guests=1):
    return client.get(
        f"/api/v1/hotels/{hotel_id}/availability",
        params={
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "guests": guests,
        },
    )


def _free_rooms(body: dict) -> dict[str, list[int]]:
    return {rt["name"]: rt["room_ids"] for rt in body["hotel"]["room_types"]}


def _add_booking(
    db: Session, user: User, room_id: int, check_in: date, check_out: date, status: str
) -> Booking:
    booking = Booking(
        user_id=user.id,
        room_id=room_id,
        check_in=check_in,
        check_out=check_out,
        guests=1,
        status=status,
        hold_expires_at=None,
        total_price=100,
        idempotency_key=f"k-{room_id}-{check_in}-{status}",
    )
    if status == "held":
        booking.hold_expires_at = db.scalar(text("SELECT now() + interval '10 minutes'"))
    db.add(booking)
    db.commit()
    return booking


def test_all_rooms_free_with_prices(client: TestClient, catalog: Catalog) -> None:
    response = _availability(client, catalog.hotel_id, day(10), day(13))
    assert response.status_code == 200
    body = response.json()

    assert body["stay"] == {
        "check_in": day(10).isoformat(),
        "check_out": day(13).isoformat(),
        "nights": 3,
        "guests": 1,
    }
    standard, suite = body["hotel"]["room_types"]
    assert standard["name"] == "Standard"
    assert standard["available_rooms"] == 2
    assert standard["total_price"] == str(STANDARD_PRICE * 3)
    assert suite["available_rooms"] == 1
    assert suite["total_price"] == str(SUITE_PRICE * 3)


def test_guest_count_filters_room_types(client: TestClient, catalog: Catalog) -> None:
    body = _availability(client, catalog.hotel_id, day(10), day(12), guests=3).json()
    assert list(_free_rooms(body)) == ["Suite"]

    body = _availability(client, catalog.hotel_id, day(10), day(12), guests=5).json()
    assert body["hotel"]["room_types"] == []


@pytest.mark.parametrize("status", ["held", "confirmed"])
def test_active_booking_hides_room(
    client: TestClient,
    db_session: Session,
    catalog: Catalog,
    make_user: Callable[..., User],
    status: str,
) -> None:
    booked = catalog.standard_rooms[0]
    _add_booking(db_session, make_user(), booked, day(10), day(15), status)

    body = _availability(client, catalog.hotel_id, day(12), day(13)).json()
    assert _free_rooms(body)["Standard"] == [catalog.standard_rooms[1]]


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_inactive_booking_does_not_hide_room(
    client: TestClient,
    db_session: Session,
    catalog: Catalog,
    make_user: Callable[..., User],
    status: str,
) -> None:
    _add_booking(db_session, make_user(), catalog.standard_rooms[0], day(10), day(15), status)
    body = _availability(client, catalog.hotel_id, day(12), day(13)).json()
    assert len(_free_rooms(body)["Standard"]) == 2


def test_lapsed_hold_does_not_hide_room(
    client: TestClient, db_session: Session, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    hold = _add_booking(
        db_session, make_user(), catalog.standard_rooms[0], day(10), day(15), "held"
    )
    db_session.execute(
        text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id = :id"),
        {"id": hold.id},
    )
    body = _availability(client, catalog.hotel_id, day(12), day(13)).json()
    assert len(_free_rooms(body)["Standard"]) == 2


def test_back_to_back_stay_is_available(
    client: TestClient, db_session: Session, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    _add_booking(db_session, make_user(), catalog.standard_rooms[0], day(10), day(15), "confirmed")
    body = _availability(client, catalog.hotel_id, day(15), day(17)).json()
    assert len(_free_rooms(body)["Standard"]) == 2


def test_unknown_hotel_is_404(client: TestClient) -> None:
    assert _availability(client, 999_999, day(10), day(12)).status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"check_in": day(5), "check_out": day(5)}, id="zero-nights"),
        pytest.param({"check_in": day(5), "check_out": day(3)}, id="reversed"),
        pytest.param({"check_in": day(-1), "check_out": day(2)}, id="past"),
        pytest.param({"check_in": day(1), "check_out": day(40)}, id="too-long"),
        pytest.param({"check_in": day(400), "check_out": day(402)}, id="beyond-horizon"),
        pytest.param({"check_in": day(1), "check_out": day(2), "guests": 0}, id="no-guests"),
        pytest.param({"check_in": day(1)}, id="missing-check-out"),
    ],
)
def test_invalid_stay_is_422(client: TestClient, catalog: Catalog, params: dict) -> None:
    query = {
        "guests": 1,
        **{k: v.isoformat() if isinstance(v, date) else v for k, v in params.items()},
    }
    response = client.get(f"/api/v1/hotels/{catalog.hotel_id}/availability", params=query)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_city_search_is_case_insensitive(client: TestClient, catalog: Catalog) -> None:
    params = {
        "city": "  PUNE ",
        "check_in": day(10).isoformat(),
        "check_out": day(12).isoformat(),
        "guests": 2,
    }
    body = client.get("/api/v1/availability", params=params).json()
    assert [h["hotel_id"] for h in body["hotels"]] == [catalog.hotel_id]

    body = client.get("/api/v1/availability", params={**params, "city": "Goa"}).json()
    assert body["hotels"] == []

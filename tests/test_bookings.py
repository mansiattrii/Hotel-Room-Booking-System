from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Payment, User
from tests.conftest import STANDARD_PRICE, Catalog, bearer, day

_keys = iter(range(1, 1_000_000))


def book(
    client: TestClient,
    headers: dict[str, str],
    room_id: int,
    check_in: date,
    check_out: date,
    guests: int = 1,
    key: str | None = None,
) -> Response:
    return client.post(
        "/api/v1/bookings",
        json={
            "room_id": room_id,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "guests": guests,
        },
        headers={**headers, "Idempotency-Key": key or f"key-{next(_keys)}"},
    )


def lapse_hold(db: Session, booking_id: int) -> None:
    """Move a hold's expiry into the past, as if HOLD_MINUTES had elapsed."""
    db.execute(
        text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id = :id"),
        {"id": booking_id},
    )
    db.expire_all()


def confirm(client: TestClient, headers: dict[str, str], booking_id: int, token="tok_visa"):
    return client.post(
        f"/api/v1/bookings/{booking_id}/confirm", json={"payment_token": token}, headers=headers
    )


def test_create_booking_places_hold(
    client: TestClient, db_session: Session, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    response = book(client, user_headers, catalog.standard_rooms[0], day(10), day(13), guests=2)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "held"
    assert Decimal(body["total_price"]) == STANDARD_PRICE * 3
    assert "Idempotent-Replayed" not in response.headers

    hold_minutes = db_session.scalar(
        text(
            "SELECT extract(epoch FROM hold_expires_at - now()) / 60 FROM bookings WHERE id = :id"
        ),
        {"id": body["id"]},
    )
    assert hold_minutes == pytest.approx(10, abs=0.1)


def test_overlapping_booking_is_409(
    client: TestClient, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    room = catalog.standard_rooms[0]
    assert book(client, bearer(make_user()), room, day(10), day(15)).status_code == 201

    response = book(client, bearer(make_user()), room, day(14), day(16))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "room_unavailable"


def test_back_to_back_booking_allowed(
    client: TestClient, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    room = catalog.standard_rooms[0]
    assert book(client, bearer(make_user()), room, day(10), day(15)).status_code == 201
    assert book(client, bearer(make_user()), room, day(15), day(18)).status_code == 201


def test_guests_over_capacity_is_422(
    client: TestClient, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    response = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12), guests=3)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "guests_exceed_capacity"


def test_unknown_room_is_404(client: TestClient, user_headers: dict[str, str]) -> None:
    assert book(client, user_headers, 999_999, day(10), day(12)).status_code == 404


@pytest.mark.parametrize(
    ("check_in", "check_out"),
    [
        pytest.param(day(-1), day(2), id="past"),
        pytest.param(day(5), day(5), id="zero-nights"),
        pytest.param(day(1), day(40), id="too-long"),
        pytest.param(day(400), day(401), id="beyond-horizon"),
    ],
)
def test_invalid_dates_are_422(
    client: TestClient,
    catalog: Catalog,
    user_headers: dict[str, str],
    check_in: date,
    check_out: date,
) -> None:
    response = book(client, user_headers, catalog.standard_rooms[0], check_in, check_out)
    assert response.status_code == 422


def test_idempotency_key_header_required(
    client: TestClient, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/bookings",
        json={
            "room_id": catalog.standard_rooms[0],
            "check_in": day(10).isoformat(),
            "check_out": day(12).isoformat(),
            "guests": 1,
        },
        headers=user_headers,
    )
    assert response.status_code == 422


def test_booking_requires_authentication(client: TestClient, catalog: Catalog) -> None:
    assert book(client, {}, catalog.standard_rooms[0], day(10), day(12)).status_code == 401


def test_same_key_returns_original_booking(
    client: TestClient, db_session: Session, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    first = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12), key="abc")
    retry = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12), key="abc")

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert db_session.scalar(text("SELECT count(*) FROM bookings")) == 1


def test_same_key_different_body_is_422(
    client: TestClient, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    book(client, user_headers, catalog.standard_rooms[0], day(10), day(12), key="abc")
    response = book(client, user_headers, catalog.standard_rooms[0], day(10), day(13), key="abc")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"


def test_idempotency_keys_are_per_user(
    client: TestClient, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    first = book(client, bearer(make_user()), catalog.standard_rooms[0], day(10), day(12), key="k")
    second = book(client, bearer(make_user()), catalog.standard_rooms[1], day(10), day(12), key="k")
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_lapsed_hold_frees_the_room(
    client: TestClient, db_session: Session, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    room = catalog.standard_rooms[0]
    first = book(client, bearer(make_user()), room, day(10), day(15)).json()
    lapse_hold(db_session, first["id"])

    second = book(client, bearer(make_user()), room, day(12), day(14))
    assert second.status_code == 201
    status = db_session.scalar(
        text("SELECT status FROM bookings WHERE id = :id"), {"id": first["id"]}
    )
    assert status == "expired"


def test_confirming_lapsed_hold_is_409(
    client: TestClient, db_session: Session, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    booking = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12)).json()
    lapse_hold(db_session, booking["id"])

    response = confirm(client, user_headers, booking["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "hold_expired"
    assert (
        client.get(f"/api/v1/bookings/{booking['id']}", headers=user_headers).json()["status"]
        == "expired"
    )


def test_confirm_records_payment_and_confirms(
    client: TestClient, db_session: Session, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    booking = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12)).json()
    response = confirm(client, user_headers, booking["id"])

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    payment = db_session.query(Payment).filter_by(booking_id=booking["id"]).one()
    assert payment.status == "succeeded"
    assert payment.amount == Decimal(booking["total_price"])


def test_declined_payment_keeps_hold_and_allows_retry(
    client: TestClient, db_session: Session, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    booking = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12)).json()

    declined = confirm(client, user_headers, booking["id"], token="tok_declined")
    assert declined.status_code == 402
    assert declined.json()["error"]["code"] == "payment_declined"
    current = client.get(f"/api/v1/bookings/{booking['id']}", headers=user_headers).json()
    assert current["status"] == "held"

    assert confirm(client, user_headers, booking["id"]).status_code == 200
    statuses = db_session.scalars(
        text("SELECT status FROM payments WHERE booking_id = :id ORDER BY id"),
        {"id": booking["id"]},
    ).all()
    assert statuses == ["failed", "succeeded"]


def test_confirming_twice_does_not_charge_twice(
    client: TestClient, db_session: Session, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    booking = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12)).json()
    assert confirm(client, user_headers, booking["id"]).status_code == 200
    again = confirm(client, user_headers, booking["id"])

    assert again.status_code == 200
    assert again.json()["status"] == "confirmed"
    count = db_session.scalar(
        text("SELECT count(*) FROM payments WHERE booking_id = :id"), {"id": booking["id"]}
    )
    assert count == 1


def test_confirming_cancelled_booking_is_409(
    client: TestClient, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    booking = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12)).json()
    client.post(f"/api/v1/bookings/{booking['id']}/cancel", headers=user_headers)
    response = confirm(client, user_headers, booking["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "booking_cancelled"


def test_cannot_confirm_someone_elses_booking(
    client: TestClient, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    owner, other = bearer(make_user()), bearer(make_user())
    booking = book(client, owner, catalog.standard_rooms[0], day(10), day(12)).json()
    assert confirm(client, other, booking["id"]).status_code == 403


def test_owner_can_cancel_and_room_is_freed(
    client: TestClient, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    owner = bearer(make_user())
    booking = book(client, owner, catalog.standard_rooms[0], day(10), day(12)).json()
    confirm(client, owner, booking["id"])

    response = client.post(f"/api/v1/bookings/{booking['id']}/cancel", headers=owner)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    rebook = book(client, bearer(make_user()), catalog.standard_rooms[0], day(10), day(12))
    assert rebook.status_code == 201


def test_cancel_twice_is_harmless(
    client: TestClient, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    booking = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12)).json()
    url = f"/api/v1/bookings/{booking['id']}/cancel"
    assert client.post(url, headers=user_headers).status_code == 200
    assert client.post(url, headers=user_headers).status_code == 200


def test_other_user_cannot_cancel(
    client: TestClient, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    owner, other = bearer(make_user()), bearer(make_user())
    booking = book(client, owner, catalog.standard_rooms[0], day(10), day(12)).json()

    response = client.post(f"/api/v1/bookings/{booking['id']}/cancel", headers=other)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    status = client.get(f"/api/v1/bookings/{booking['id']}", headers=owner).json()["status"]
    assert status == "held"


def test_admin_can_cancel_any_booking(
    client: TestClient,
    catalog: Catalog,
    user_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    booking = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12)).json()
    response = client.post(f"/api/v1/bookings/{booking['id']}/cancel", headers=admin_headers)
    assert response.status_code == 200


def test_cancelling_expired_booking_is_409(
    client: TestClient, db_session: Session, catalog: Catalog, user_headers: dict[str, str]
) -> None:
    booking = book(client, user_headers, catalog.standard_rooms[0], day(10), day(12)).json()
    db_session.execute(
        text("UPDATE bookings SET status = 'expired' WHERE id = :id"), {"id": booking["id"]}
    )
    db_session.expire_all()
    response = client.post(f"/api/v1/bookings/{booking['id']}/cancel", headers=user_headers)
    assert response.status_code == 409


def test_get_booking_access_rules(
    client: TestClient,
    catalog: Catalog,
    make_user: Callable[..., User],
    admin_headers: dict[str, str],
) -> None:
    owner, other = bearer(make_user()), bearer(make_user())
    booking = book(client, owner, catalog.standard_rooms[0], day(10), day(12)).json()
    url = f"/api/v1/bookings/{booking['id']}"

    assert client.get(url, headers=owner).status_code == 200
    assert client.get(url, headers=admin_headers).status_code == 200
    assert client.get(url, headers=other).status_code == 403
    assert client.get("/api/v1/bookings/999999", headers=owner).status_code == 404
    assert client.get(url).status_code == 401


def test_my_bookings_lists_only_mine_newest_first(
    client: TestClient, catalog: Catalog, make_user: Callable[..., User]
) -> None:
    me, other = bearer(make_user()), bearer(make_user())
    mine = [
        book(client, me, catalog.standard_rooms[0], day(10 + 3 * i), day(12 + 3 * i)).json()["id"]
        for i in range(3)
    ]
    book(client, other, catalog.standard_rooms[1], day(10), day(12))

    first_page = client.get("/api/v1/bookings/me", params={"page_size": 2}, headers=me).json()
    second_page = client.get(
        "/api/v1/bookings/me", params={"page": 2, "page_size": 2}, headers=me
    ).json()
    assert first_page["total"] == 3
    ids = [b["id"] for b in first_page["items"] + second_page["items"]]
    assert ids == sorted(mine, reverse=True)

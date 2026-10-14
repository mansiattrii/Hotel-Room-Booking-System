"""Public hotel endpoints and admin catalog management."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Hotel, RoomType


@pytest.fixture
def hotels(db_session: Session) -> list[Hotel]:
    rows = [
        Hotel(name=f"Hotel {i}", city=city, address="Addr", star_rating=3)
        for i, city in enumerate(["Pune"] * 5 + ["Goa"] * 2 + ["New Delhi"], start=1)
    ]
    db_session.add_all(rows)
    db_session.commit()
    return rows


def test_list_hotels_paginates_in_id_order(client: TestClient, hotels: list[Hotel]) -> None:
    first = client.get("/api/v1/hotels", params={"page": 1, "page_size": 3}).json()
    second = client.get("/api/v1/hotels", params={"page": 2, "page_size": 3}).json()
    last = client.get("/api/v1/hotels", params={"page": 3, "page_size": 3}).json()

    assert first["total"] == second["total"] == last["total"] == 8
    ids = [h["id"] for page in (first, second, last) for h in page["items"]]
    assert ids == sorted(h.id for h in hotels)
    assert len(last["items"]) == 2


def test_page_past_the_end_is_empty(client: TestClient, hotels: list[Hotel]) -> None:
    body = client.get("/api/v1/hotels", params={"page": 50}).json()
    assert body["items"] == []
    assert body["total"] == 8


@pytest.mark.parametrize(
    ("city", "expected"), [("pune", 5), ("GOA", 2), ("  new delhi ", 1), ("Mumbai", 0)]
)
def test_city_filter_is_case_insensitive(
    client: TestClient, hotels: list[Hotel], city: str, expected: int
) -> None:
    body = client.get("/api/v1/hotels", params={"city": city}).json()
    assert body["total"] == expected
    assert len(body["items"]) == expected


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"city": ""}, {"page": "abc"}],
)
def test_list_hotels_rejects_bad_query(client: TestClient, params: dict[str, object]) -> None:
    response = client.get("/api/v1/hotels", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_hotel_detail_lists_room_types_cheapest_first(
    client: TestClient, db_session: Session, hotels: list[Hotel]
) -> None:
    hotel = hotels[0]
    db_session.add_all(
        [
            RoomType(hotel_id=hotel.id, name="Suite", capacity=4, base_price_per_night=9000),
            RoomType(hotel_id=hotel.id, name="Standard", capacity=2, base_price_per_night=2500),
        ]
    )
    db_session.commit()

    body = client.get(f"/api/v1/hotels/{hotel.id}").json()
    assert body["name"] == hotel.name
    assert [rt["name"] for rt in body["room_types"]] == ["Standard", "Suite"]
    assert Decimal(body["room_types"][0]["base_price_per_night"]) == Decimal("2500.00")


def test_unknown_hotel_is_404(client: TestClient) -> None:
    response = client.get("/api/v1/hotels/999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def _create_hotel(client: TestClient, headers: dict[str, str]) -> int:
    response = client.post(
        "/api/v1/admin/hotels",
        json={"name": " Sea View ", "city": "Goa", "address": "Beach Rd", "star_rating": 4},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["id"]


def _create_room_type(client: TestClient, headers: dict[str, str], hotel_id: int) -> int:
    response = client.post(
        f"/api/v1/admin/hotels/{hotel_id}/room-types",
        json={"name": "Deluxe", "capacity": 2, "base_price_per_night": "4500.50"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_admin_builds_a_hotel(client: TestClient, admin_headers: dict[str, str]) -> None:
    hotel_id = _create_hotel(client, admin_headers)
    room_type_id = _create_room_type(client, admin_headers, hotel_id)
    response = client.post(
        f"/api/v1/admin/hotels/{hotel_id}/rooms",
        json={"room_type_id": room_type_id, "room_number": "101"},
        headers=admin_headers,
    )
    assert response.status_code == 201
    assert response.json()["hotel_id"] == hotel_id

    detail = client.get(f"/api/v1/hotels/{hotel_id}").json()
    assert detail["name"] == "Sea View"
    assert detail["room_types"][0]["base_price_per_night"] == "4500.50"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"name": "H", "city": "Goa", "address": "A", "star_rating": 6}, id="stars"),
        pytest.param({"name": "   ", "city": "Goa", "address": "A", "star_rating": 3}, id="blank"),
        pytest.param({"name": "H", "city": "Goa", "star_rating": 3}, id="missing-address"),
    ],
)
def test_create_hotel_validation(
    client: TestClient, admin_headers: dict[str, str], payload: dict[str, object]
) -> None:
    response = client.post("/api/v1/admin/hotels", json=payload, headers=admin_headers)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "price", [pytest.param("0", id="zero"), pytest.param("10.555", id="three-decimals")]
)
def test_room_type_price_validation(
    client: TestClient, admin_headers: dict[str, str], price: str
) -> None:
    hotel_id = _create_hotel(client, admin_headers)
    response = client.post(
        f"/api/v1/admin/hotels/{hotel_id}/room-types",
        json={"name": "Deluxe", "capacity": 2, "base_price_per_night": price},
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_room_type_for_unknown_hotel_is_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/admin/hotels/999999/room-types",
        json={"name": "Deluxe", "capacity": 2, "base_price_per_night": "1000"},
        headers=admin_headers,
    )
    assert response.status_code == 404


def test_duplicate_room_type_name_is_409(client: TestClient, admin_headers: dict[str, str]) -> None:
    hotel_id = _create_hotel(client, admin_headers)
    _create_room_type(client, admin_headers, hotel_id)
    response = client.post(
        f"/api/v1/admin/hotels/{hotel_id}/room-types",
        json={"name": "Deluxe", "capacity": 3, "base_price_per_night": "5000"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "room_type_exists"


def test_room_with_another_hotels_room_type_is_422(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    hotel_a = _create_hotel(client, admin_headers)
    hotel_b = _create_hotel(client, admin_headers)
    type_of_b = _create_room_type(client, admin_headers, hotel_b)

    response = client.post(
        f"/api/v1/admin/hotels/{hotel_a}/rooms",
        json={"room_type_id": type_of_b, "room_number": "101"},
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_room_type"


def test_duplicate_room_number_is_409(client: TestClient, admin_headers: dict[str, str]) -> None:
    hotel_id = _create_hotel(client, admin_headers)
    room_type_id = _create_room_type(client, admin_headers, hotel_id)
    payload = {"room_type_id": room_type_id, "room_number": "101"}
    url = f"/api/v1/admin/hotels/{hotel_id}/rooms"

    assert client.post(url, json=payload, headers=admin_headers).status_code == 201
    response = client.post(url, json=payload, headers=admin_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "room_exists"

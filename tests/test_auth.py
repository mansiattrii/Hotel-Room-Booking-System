from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.security import create_access_token
from tests.conftest import TEST_PASSWORD, bearer

ADMIN_ENDPOINTS = [
    ("/api/v1/admin/hotels", {"name": "H", "city": "Pune", "address": "A", "star_rating": 3}),
    (
        "/api/v1/admin/hotels/1/room-types",
        {"name": "Deluxe", "capacity": 2, "base_price_per_night": "3000.00"},
    ),
    ("/api/v1/admin/hotels/1/rooms", {"room_type_id": 1, "room_number": "101"}),
]


def test_register_creates_user_without_exposing_hash(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register", json={"email": "New@Example.com", "password": TEST_PASSWORD}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new@example.com"
    assert body["role"] == "user"
    assert "password" not in body and "password_hash" not in body


def test_register_duplicate_email_is_case_insensitive(client: TestClient) -> None:
    payload = {"email": "dup@example.com", "password": TEST_PASSWORD}
    assert client.post("/api/v1/auth/register", json=payload).status_code == 201

    response = client.post("/api/v1/auth/register", json={**payload, "email": "DUP@example.com"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"email": "not-an-email", "password": TEST_PASSWORD}, id="bad-email"),
        pytest.param({"email": "a@example.com", "password": "short"}, id="short-password"),
        pytest.param({"email": "a@example.com", "password": "x" * 129}, id="long-password"),
        pytest.param({"email": "a@example.com"}, id="missing-password"),
    ],
)
def test_register_validation(client: TestClient, payload: dict[str, str]) -> None:
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_register_ignores_role_in_body(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "sneaky@example.com", "password": TEST_PASSWORD, "role": "admin"},
    )
    assert response.status_code == 201
    assert response.json()["role"] == "user"


def test_login_returns_usable_token(client: TestClient, make_user: Callable[..., User]) -> None:
    make_user(role="admin", email="boss@example.com")
    response = client.post(
        "/api/v1/auth/login", json={"email": "Boss@Example.com", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0

    headers = {"Authorization": f"Bearer {body['access_token']}"}
    path, payload = ADMIN_ENDPOINTS[0]
    assert client.post(path, json=payload, headers=headers).status_code == 201


@pytest.mark.parametrize(
    ("email", "password"),
    [
        pytest.param("known@example.com", "wrong-password", id="wrong-password"),
        pytest.param("unknown@example.com", TEST_PASSWORD, id="unknown-email"),
    ],
)
def test_login_failures_are_indistinguishable(
    client: TestClient, make_user: Callable[..., User], email: str, password: str
) -> None:
    make_user(email="known@example.com")
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "invalid_credentials", "message": "Invalid email or password"}
    }


def _expired(user: User) -> dict[str, str]:
    token = create_access_token(user.id, expires_delta=timedelta(seconds=-1))
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "make_headers",
    [
        pytest.param(lambda _: {}, id="missing"),
        pytest.param(lambda _: {"Authorization": "Bearer not.a.jwt"}, id="malformed"),
        pytest.param(lambda _: {"Authorization": "Basic dXNlcjpwYXNz"}, id="wrong-scheme"),
        pytest.param(_expired, id="expired"),
    ],
)
def test_protected_endpoint_rejects_bad_credentials(
    client: TestClient,
    make_user: Callable[..., User],
    make_headers: Callable[[User], dict[str, str]],
) -> None:
    admin = make_user(role="admin")
    path, payload = ADMIN_ENDPOINTS[0]
    response = client.post(path, json=payload, headers=make_headers(admin))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


def test_token_of_deleted_user_rejected(
    client: TestClient, db_session: Session, make_user: Callable[..., User]
) -> None:
    admin = make_user(role="admin")
    headers = bearer(admin)
    db_session.delete(admin)
    db_session.commit()

    path, payload = ADMIN_ENDPOINTS[0]
    assert client.post(path, json=payload, headers=headers).status_code == 401


def test_demoted_admin_loses_access_immediately(
    client: TestClient, db_session: Session, make_user: Callable[..., User]
) -> None:
    admin = make_user(role="admin")
    headers = bearer(admin)
    admin.role = "user"
    db_session.commit()

    path, payload = ADMIN_ENDPOINTS[0]
    assert client.post(path, json=payload, headers=headers).status_code == 403


@pytest.mark.parametrize(("path", "payload"), ADMIN_ENDPOINTS)
def test_non_admin_forbidden_on_admin_endpoints(
    client: TestClient, user_headers: dict[str, str], path: str, payload: dict[str, object]
) -> None:
    response = client.post(path, json=payload, headers=user_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import build_engine, get_db
from app.main import app
from app.models import Hotel, Room, RoomType, User
from app.security import create_access_token
from app.services import users

ROOT = Path(__file__).resolve().parent.parent
TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = get_settings().test_database_url
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set; see .env.example")
    return url


@pytest.fixture(scope="session")
def engine(test_database_url: str) -> Iterator[Engine]:
    """Rebuild the test schema from the migrations once per test session."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = test_database_url
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = build_engine(test_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """A connection whose outer transaction is rolled back after each test."""
    with engine.connect() as conn:
        trans = conn.begin()
        yield conn
        trans.rollback()


@pytest.fixture
def db_session(connection: Connection) -> Iterator[Session]:
    """A session bound to the test transaction.

    "create_savepoint" turns the services' commit() into RELEASE SAVEPOINT, so their
    transaction logic runs unchanged while the outer rollback still discards everything.
    """
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        autoflush=False,
        expire_on_commit=False,
    )
    yield session
    session.close()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def make_user(db_session: Session) -> Callable[..., User]:
    counter = iter(range(1, 10_000))

    def _make(role: str = "user", email: str | None = None) -> User:
        address = email or f"{role}{next(counter)}@example.com"
        return users.register_user(db_session, address, TEST_PASSWORD, role=role)

    return _make


def bearer(user: User) -> dict[str, str]:
    """Authorization header for a user."""
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def user_headers(make_user: Callable[..., User]) -> dict[str, str]:
    return bearer(make_user())


@pytest.fixture
def admin_headers(make_user: Callable[..., User]) -> dict[str, str]:
    return bearer(make_user(role="admin"))


def day(offset: int) -> date:
    """A date relative to today (UTC, matching the API's validation)."""
    return datetime.now(UTC).date() + timedelta(days=offset)


@dataclass(frozen=True)
class Catalog:
    hotel_id: int
    standard_type_id: int
    suite_type_id: int
    standard_rooms: tuple[int, int]
    suite_room: int


STANDARD_PRICE = Decimal("2500.00")
SUITE_PRICE = Decimal("9000.00")


@pytest.fixture
def catalog(db_session: Session) -> Catalog:
    """One hotel in Pune: two Standard rooms (2 guests) and one Suite (4 guests)."""
    hotel = Hotel(name="Test Hotel", city="Pune", address="1 Test Road", star_rating=4)
    db_session.add(hotel)
    db_session.flush()
    standard = RoomType(
        hotel_id=hotel.id, name="Standard", capacity=2, base_price_per_night=STANDARD_PRICE
    )
    suite = RoomType(hotel_id=hotel.id, name="Suite", capacity=4, base_price_per_night=SUITE_PRICE)
    db_session.add_all([standard, suite])
    db_session.flush()
    rooms = [
        Room(hotel_id=hotel.id, room_type_id=standard.id, room_number="101"),
        Room(hotel_id=hotel.id, room_type_id=standard.id, room_number="102"),
        Room(hotel_id=hotel.id, room_type_id=suite.id, room_number="201"),
    ]
    db_session.add_all(rooms)
    db_session.commit()
    return Catalog(
        hotel_id=hotel.id,
        standard_type_id=standard.id,
        suite_type_id=suite.id,
        standard_rooms=(rooms[0].id, rooms[1].id),
        suite_room=rooms[2].id,
    )

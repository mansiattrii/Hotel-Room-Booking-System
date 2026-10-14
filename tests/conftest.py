from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import User
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

    engine = create_engine(test_database_url)
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

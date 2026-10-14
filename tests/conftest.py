from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine

from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent


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

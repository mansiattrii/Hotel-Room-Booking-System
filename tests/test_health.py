import socket
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import build_engine, get_db
from app.main import app


def _override_with(engine: Engine) -> None:
    def _get_db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = _get_db


def test_health_ok(engine: Engine) -> None:
    _override_with(engine)
    try:
        response = TestClient(app).get("/api/v1/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_returns_503_when_database_never_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    # A socket that completes the TCP handshake but never speaks the PostgreSQL protocol.
    # This is what a stopped container looks like behind Docker Desktop's port proxy.
    monkeypatch.setattr(get_settings(), "db_connect_timeout_seconds", 2)
    with socket.create_server(("127.0.0.1", 0)) as silent_server:
        port = silent_server.getsockname()[1]
        engine = build_engine(f"postgresql+psycopg://nobody:nothing@127.0.0.1:{port}/none")
        _override_with(engine)
        started = time.monotonic()
        try:
            response = TestClient(app).get("/api/v1/health")
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert elapsed < 10

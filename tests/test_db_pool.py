"""More concurrent requests than pool connections must queue, not deadlock.

Sync endpoints run on a thread pool (40 threads by default). Before the fix, requests beyond
the pool size blocked *inside* those threads waiting for a connection; once every thread was
blocked, the requests holding connections could not get a thread to serialise their
response, so nothing returned a connection until the pool timeout fired (503s).
"""

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import create_app

POOL_SIZE = 2
REQUESTS = 60  # more than the default 40 worker threads plus the pool size


def test_requests_beyond_pool_capacity_queue_instead_of_deadlocking(
    test_database_url: str,
) -> None:
    engine = create_engine(test_database_url, pool_size=POOL_SIZE, max_overflow=0, pool_timeout=3)
    app = create_app(
        session_factory=sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
        db_session_slots=POOL_SIZE,
    )
    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=REQUESTS) as pool:
            statuses = list(
                pool.map(lambda _: client.get("/api/v1/health").status_code, range(REQUESTS))
            )
    finally:
        engine.dispose()

    assert statuses == [200] * REQUESTS

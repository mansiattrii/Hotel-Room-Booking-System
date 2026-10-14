"""Background worker that expires lapsed holds.

    python -m app.worker

Runs as its own process (its own container in docker-compose). Correctness does not
depend on it: the booking path expires stale holds itself and availability ignores them.
The worker keeps the stored status accurate for reporting and for GET /bookings.
"""

import logging
import signal
import threading
from types import FrameType

from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import SessionLocal
from app.logging_config import configure_logging
from app.services.booking import expire_lapsed_holds

logger = logging.getLogger("app.worker")


def run_once() -> int:
    """Expire every lapsed hold once. Returns how many were expired."""
    with SessionLocal() as db:
        return expire_lapsed_holds(db)


def run_forever(stop: threading.Event, interval_seconds: float) -> None:
    """Run until `stop` is set. A failed sweep is logged and retried next interval."""
    logger.info("worker started", extra={"interval_seconds": interval_seconds})
    while not stop.is_set():
        try:
            expired = run_once()
            if expired:
                logger.info("expired lapsed holds", extra={"count": expired})
        except SQLAlchemyError:
            logger.exception("sweep failed; will retry")
        stop.wait(interval_seconds)
    logger.info("worker stopped")


def main() -> None:
    """Entry point: run until SIGINT or SIGTERM (what `docker stop` sends)."""
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("shutdown requested", extra={"signal": signal.Signals(signum).name})
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    run_forever(stop, settings.worker_interval_seconds)


if __name__ == "__main__":
    main()

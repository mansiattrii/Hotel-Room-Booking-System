"""TEST-ONLY: drop or restore the bookings exclusion constraint.

    python -m scripts.exclusion_toggle status|off|on

The naive and pessimistic strategies can only be judged on their own if the exclusion
constraint isn't silently backing them up, so the concurrency tests and experiment
remove it temporarily. This is deliberately not a migration: the production migration
path always creates the constraint. Every function refuses to touch a database whose
name doesn't end in "_test", and refuses outright when APP_ENV is production.
"""

import argparse
import logging
import sys

from sqlalchemy import Connection, text

from app.config import get_settings
from app.db import build_engine
from app.logging_config import configure_logging

logger = logging.getLogger("scripts.exclusion_toggle")

CONSTRAINT_NAME = "ex_bookings_no_overlap"

# Must stay identical to the definition in migration 0001; a test compares them.
_ADD_CONSTRAINT = f"""
ALTER TABLE bookings
    ADD CONSTRAINT {CONSTRAINT_NAME}
    EXCLUDE USING gist (
        room_id WITH =,
        daterange(check_in, check_out, '[)') WITH &&
    )
    WHERE (status IN ('held', 'confirmed'))
"""


class UnsafeDatabaseError(RuntimeError):
    """Raised instead of altering a database that isn't a test database."""


def ensure_test_database(conn: Connection) -> None:
    """Raise UnsafeDatabaseError unless conn points at a test database outside production."""
    if get_settings().app_env == "production":
        raise UnsafeDatabaseError("refusing to alter constraints when APP_ENV=production")
    name = conn.scalar(text("SELECT current_database()"))
    if not str(name).endswith("_test"):
        raise UnsafeDatabaseError(f"refusing to alter constraints on database {name!r}")


def is_enabled(conn: Connection) -> bool:
    """True if the exclusion constraint currently exists."""
    return bool(
        conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = :name)"),
            {"name": CONSTRAINT_NAME},
        )
    )


def disable(conn: Connection) -> None:
    """Drop the exclusion constraint (and its GiST index) from a test database."""
    ensure_test_database(conn)
    conn.execute(text(f"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME}"))


def enable(conn: Connection) -> None:
    """Recreate the exclusion constraint on a test database if it is missing.

    Fails if overlapping active bookings exist, which is the point: the data must be
    cleaned up before the guarantee can be restored.
    """
    ensure_test_database(conn)
    if not is_enabled(conn):
        conn.execute(text(_ADD_CONSTRAINT))


def count_overlapping_active_pairs(conn: Connection) -> int:
    """Pairs of held/confirmed bookings of the same room whose dates overlap.

    The ground truth for the experiments: anything above zero is a double booking.
    """
    return int(
        conn.scalar(
            text(
                """
                SELECT count(*)
                FROM bookings a
                JOIN bookings b ON b.room_id = a.room_id AND b.id > a.id
                WHERE a.status IN ('held', 'confirmed')
                  AND b.status IN ('held', 'confirmed')
                  AND daterange(a.check_in, a.check_out, '[)')
                      && daterange(b.check_in, b.check_out, '[)')
                """
            )
        )
    )


def main() -> int:
    """Entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(description="Toggle the exclusion constraint (test DB).")
    parser.add_argument("action", choices=["status", "off", "on"])
    parser.add_argument("--database-url", help="Defaults to TEST_DATABASE_URL.")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    url = args.database_url or settings.test_database_url
    if not url:
        logger.error("no database URL; set TEST_DATABASE_URL or pass --database-url")
        return 2

    engine = build_engine(url)
    try:
        with engine.begin() as conn:
            if args.action == "off":
                disable(conn)
            elif args.action == "on":
                enable(conn)
            logger.info("exclusion constraint", extra={"enabled": is_enabled(conn)})
    except UnsafeDatabaseError as exc:
        logger.error(str(exc))
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())

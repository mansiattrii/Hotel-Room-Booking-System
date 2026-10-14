from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def build_engine(database_url: str) -> Engine:
    """Create an engine with the pool settings used across the app."""
    settings = get_settings()
    return create_engine(
        database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        # PostgreSQL's default, stated explicitly because the booking strategies depend on
        # it: each statement sees data committed before it started. See NOTES.md.
        isolation_level="READ COMMITTED",
        # Without a timeout, a port that accepts TCP but has no server behind it
        # (e.g. Docker's port proxy while the db container is stopped) blocks forever.
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
    )


engine = build_engine(get_settings().database_url)

# expire_on_commit=False lets routers serialise ORM objects after the service commits.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def violated_constraint(exc: IntegrityError) -> str | None:
    """Name of the constraint behind an IntegrityError, as reported by PostgreSQL."""
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding one session per request."""
    with SessionLocal() as session:
        yield session

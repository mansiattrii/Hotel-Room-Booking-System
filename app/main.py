from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import SessionLocal, pool_capacity
from app.errors import register_error_handlers
from app.logging_config import configure_logging
from app.routers import admin, auth, availability, bookings, health, hotels

API_PREFIX = "/api/v1"


def create_app(
    session_factory: sessionmaker[Session] = SessionLocal,
    db_session_slots: int | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    db_session_slots caps how many requests hold a database session at once; it must not
    exceed what the session factory's pool can provide (defaults to the app pool's capacity).
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    slots = db_session_slots or pool_capacity()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Created here so the semaphore belongs to the event loop that serves requests.
        app.state.db_session_slots = anyio.Semaphore(slots)
        yield

    app = FastAPI(title="Hotel Booking API", version="0.1.0", lifespan=lifespan)
    app.state.session_factory = session_factory
    register_error_handlers(app)
    routers = (
        health.router,
        auth.router,
        hotels.router,
        availability.router,
        bookings.router,
        admin.router,
    )
    for router in routers:
        app.include_router(router, prefix=API_PREFIX)
    return app


app = create_app()

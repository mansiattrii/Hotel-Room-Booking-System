from fastapi import FastAPI

from app.config import get_settings
from app.errors import register_error_handlers
from app.logging_config import configure_logging
from app.routers import health

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Hotel Booking API", version="0.1.0")
    register_error_handlers(app)
    app.include_router(health.router, prefix=API_PREFIX)
    return app


app = create_app()

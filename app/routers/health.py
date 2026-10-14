import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import AppError
from app.schemas import ErrorResponse, HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}},
)
def health(db: Session = Depends(get_db)) -> HealthResponse:
    """Report whether the API can reach the database."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning("health check failed", extra={"error": type(exc).__name__})
        raise AppError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "database_unavailable", "Database is unreachable"
        ) from exc
    return HealthResponse(status="ok", database="ok")

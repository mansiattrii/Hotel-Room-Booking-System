from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_admin
from app.schemas import (
    ErrorResponse,
    HotelCreate,
    HotelOut,
    RoomCreate,
    RoomOut,
    RoomTypeCreate,
    RoomTypeOut,
)
from app.services import catalog

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
        status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
    },
)


@router.post("/hotels", response_model=HotelOut, status_code=status.HTTP_201_CREATED)
def create_hotel(body: HotelCreate, db: Session = Depends(get_db)) -> HotelOut:
    """Create a hotel."""
    return HotelOut.model_validate(catalog.create_hotel(db, body))


@router.post(
    "/hotels/{hotel_id}/room-types",
    response_model=RoomTypeOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
def create_room_type(
    hotel_id: int, body: RoomTypeCreate, db: Session = Depends(get_db)
) -> RoomTypeOut:
    """Add a room type to a hotel."""
    return RoomTypeOut.model_validate(catalog.create_room_type(db, hotel_id, body))


@router.post(
    "/hotels/{hotel_id}/rooms",
    response_model=RoomOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
def create_room(hotel_id: int, body: RoomCreate, db: Session = Depends(get_db)) -> RoomOut:
    """Add a room to a hotel. The room type must belong to the same hotel."""
    return RoomOut.model_validate(catalog.create_room(db, hotel_id, body))

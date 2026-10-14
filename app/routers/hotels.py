from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ErrorResponse, HotelDetail, HotelOut, Page, RoomTypeOut
from app.services import catalog

router = APIRouter(prefix="/hotels", tags=["hotels"])


@router.get("", response_model=Page[HotelOut])
def list_hotels(
    city: str | None = Query(default=None, min_length=1, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> Page[HotelOut]:
    """List hotels, optionally filtered by city (case-insensitive)."""
    hotels, total = catalog.list_hotels(db, city, page, page_size)
    return Page[HotelOut](
        items=[HotelOut.model_validate(h) for h in hotels],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{hotel_id}",
    response_model=HotelDetail,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
def get_hotel(hotel_id: int, db: Session = Depends(get_db)) -> HotelDetail:
    """A hotel with its room types."""
    hotel = catalog.get_hotel(db, hotel_id)
    room_types = catalog.list_room_types(db, hotel_id)
    return HotelDetail(
        **HotelOut.model_validate(hotel).model_dump(),
        room_types=[RoomTypeOut.model_validate(rt) for rt in room_types],
    )

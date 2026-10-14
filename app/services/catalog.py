import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import violated_constraint
from app.errors import ConflictError, InvalidInputError, NotFoundError
from app.models import Hotel, Room, RoomType
from app.schemas import HotelCreate, RoomCreate, RoomTypeCreate

logger = logging.getLogger(__name__)


def list_hotels(
    db: Session, city: str | None, page: int, page_size: int
) -> tuple[list[Hotel], int]:
    """One page of hotels, optionally filtered by city (case-insensitive), plus the total."""
    filters = []
    if city is not None:
        filters.append(func.lower(Hotel.city) == city.strip().lower())

    total = db.scalar(select(func.count()).select_from(Hotel).where(*filters)) or 0
    # Ordering by the primary key keeps pages stable: without ORDER BY, PostgreSQL may
    # return rows in any order and a hotel could appear on two pages or none.
    hotels = db.scalars(
        select(Hotel)
        .where(*filters)
        .order_by(Hotel.id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    return list(hotels), total


def get_hotel(db: Session, hotel_id: int) -> Hotel:
    """Fetch a hotel or raise NotFoundError."""
    hotel = db.get(Hotel, hotel_id)
    if hotel is None:
        raise NotFoundError(f"Hotel {hotel_id} not found")
    return hotel


def list_room_types(db: Session, hotel_id: int) -> list[RoomType]:
    """All room types of a hotel, cheapest first."""
    return list(
        db.scalars(
            select(RoomType)
            .where(RoomType.hotel_id == hotel_id)
            .order_by(RoomType.base_price_per_night, RoomType.id)
        ).all()
    )


def create_hotel(db: Session, data: HotelCreate) -> Hotel:
    """Insert a hotel."""
    hotel = Hotel(**data.model_dump())
    db.add(hotel)
    db.commit()
    logger.info("hotel created", extra={"hotel_id": hotel.id})
    return hotel


def create_room_type(db: Session, hotel_id: int, data: RoomTypeCreate) -> RoomType:
    """Add a room type to a hotel. Raises NotFoundError or ConflictError."""
    get_hotel(db, hotel_id)
    room_type = RoomType(hotel_id=hotel_id, **data.model_dump())
    db.add(room_type)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if violated_constraint(exc) == "uq_room_types_hotel_id_name":
            raise ConflictError(
                f"Hotel {hotel_id} already has a room type named {data.name!r}",
                code="room_type_exists",
            ) from exc
        raise
    logger.info("room type created", extra={"hotel_id": hotel_id, "room_type_id": room_type.id})
    return room_type


def create_room(db: Session, hotel_id: int, data: RoomCreate) -> Room:
    """Add a room to a hotel. Raises NotFoundError, InvalidInputError or ConflictError."""
    get_hotel(db, hotel_id)
    room = Room(hotel_id=hotel_id, **data.model_dump())
    db.add(room)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        constraint = violated_constraint(exc)
        if constraint == "fk_rooms_hotel_id_room_type_id_room_types":
            raise InvalidInputError(
                f"Room type {data.room_type_id} does not belong to hotel {hotel_id}",
                code="invalid_room_type",
            ) from exc
        if constraint == "uq_rooms_hotel_id_room_number":
            raise ConflictError(
                f"Hotel {hotel_id} already has a room numbered {data.room_number!r}",
                code="room_exists",
            ) from exc
        raise
    logger.info("room created", extra={"hotel_id": hotel_id, "room_id": room.id})
    return room

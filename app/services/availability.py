"""Room availability, as explicit SQL.

A room is available for [check_in, check_out) when no *active* booking of that room
overlaps the range. Active means confirmed, or held with a hold that hasn't expired yet.
Holds past their expiry count as free even before the worker marks them expired.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, Date, Integer, String, bindparam, text
from sqlalchemy.orm import Session

from app.services.catalog import get_hotel

_AVAILABILITY_SQL = """
SELECT
    h.id                                   AS hotel_id,
    h.name                                 AS hotel_name,
    h.city                                 AS city,
    h.address                              AS address,
    h.star_rating                          AS star_rating,
    rt.id                                  AS room_type_id,
    rt.name                                AS room_type_name,
    rt.capacity                            AS capacity,
    rt.base_price_per_night                AS price_per_night,
    rt.base_price_per_night * :nights      AS total_price,
    array_agg(r.id ORDER BY r.room_number) AS room_ids
FROM hotels h
JOIN room_types rt ON rt.hotel_id = h.id
JOIN rooms r       ON r.hotel_id = rt.hotel_id AND r.room_type_id = rt.id
WHERE {hotel_filter}
  AND rt.capacity >= :guests
  AND NOT EXISTS (
        SELECT 1
        FROM bookings b
        WHERE b.room_id = r.id
          AND b.status IN ('held', 'confirmed')
          AND daterange(b.check_in, b.check_out, '[)') && daterange(:check_in, :check_out, '[)')
          AND (b.status = 'confirmed' OR b.hold_expires_at > now())
      )
GROUP BY h.id, rt.id
ORDER BY h.id, rt.base_price_per_night, rt.id
"""

# The filter is chosen from these fixed strings, never built from user input.
_BY_HOTEL = text(_AVAILABILITY_SQL.format(hotel_filter="h.id = :hotel_id"))
_BY_CITY = text(_AVAILABILITY_SQL.format(hotel_filter="lower(h.city) = lower(:city)"))

_PARAM_TYPES = [
    bindparam("check_in", type_=Date),
    bindparam("check_out", type_=Date),
    bindparam("guests", type_=Integer),
    bindparam("nights", type_=Integer),
]
BY_HOTEL_QUERY = _BY_HOTEL.bindparams(*_PARAM_TYPES, bindparam("hotel_id", type_=BigInteger))
BY_CITY_QUERY = _BY_CITY.bindparams(*_PARAM_TYPES, bindparam("city", type_=String))


@dataclass(frozen=True)
class RoomTypeAvailability:
    room_type_id: int
    name: str
    capacity: int
    price_per_night: Decimal
    total_price: Decimal
    room_ids: list[int]


@dataclass
class HotelAvailability:
    hotel_id: int
    name: str
    city: str
    address: str
    star_rating: int
    room_types: list[RoomTypeAvailability] = field(default_factory=list)


def _params(check_in: date, check_out: date, guests: int) -> dict[str, object]:
    return {
        "check_in": check_in,
        "check_out": check_out,
        "guests": guests,
        "nights": (check_out - check_in).days,
    }


def _group_by_hotel(rows: list) -> list[HotelAvailability]:
    hotels: dict[int, HotelAvailability] = {}
    for row in rows:
        hotel = hotels.get(row.hotel_id)
        if hotel is None:
            hotel = hotels[row.hotel_id] = HotelAvailability(
                hotel_id=row.hotel_id,
                name=row.hotel_name,
                city=row.city,
                address=row.address,
                star_rating=row.star_rating,
            )
        hotel.room_types.append(
            RoomTypeAvailability(
                room_type_id=row.room_type_id,
                name=row.room_type_name,
                capacity=row.capacity,
                price_per_night=row.price_per_night,
                total_price=row.total_price,
                room_ids=list(row.room_ids),
            )
        )
    return list(hotels.values())


def hotel_availability(
    db: Session, hotel_id: int, check_in: date, check_out: date, guests: int
) -> HotelAvailability:
    """Available rooms of one hotel, grouped by room type. Raises NotFoundError."""
    hotel = get_hotel(db, hotel_id)
    rows = db.execute(
        BY_HOTEL_QUERY, {**_params(check_in, check_out, guests), "hotel_id": hotel_id}
    ).all()
    grouped = _group_by_hotel(rows)
    if grouped:
        return grouped[0]
    return HotelAvailability(
        hotel_id=hotel.id,
        name=hotel.name,
        city=hotel.city,
        address=hotel.address,
        star_rating=hotel.star_rating,
    )


def city_availability(
    db: Session, city: str, check_in: date, check_out: date, guests: int
) -> list[HotelAvailability]:
    """Hotels in a city (case-insensitive) that have at least one suitable free room."""
    rows = db.execute(
        BY_CITY_QUERY, {**_params(check_in, check_out, guests), "city": city.strip()}
    ).all()
    return _group_by_hotel(rows)

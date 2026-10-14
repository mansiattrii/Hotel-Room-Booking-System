from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    CityAvailabilityQuery,
    CityAvailabilityResponse,
    ErrorResponse,
    HotelAvailabilityOut,
    HotelAvailabilityResponse,
    StayRequest,
    StaySummary,
)
from app.services import availability

router = APIRouter(tags=["availability"])


def _summary(stay: StayRequest) -> StaySummary:
    return StaySummary(
        check_in=stay.check_in, check_out=stay.check_out, nights=stay.nights, guests=stay.guests
    )


@router.get(
    "/hotels/{hotel_id}/availability",
    response_model=HotelAvailabilityResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
def hotel_availability(
    hotel_id: int,
    stay: Annotated[StayRequest, Query()],
    db: Session = Depends(get_db),
) -> HotelAvailabilityResponse:
    """Free rooms of one hotel for the stay, grouped by room type, with the total price."""
    result = availability.hotel_availability(
        db, hotel_id, stay.check_in, stay.check_out, stay.guests
    )
    return HotelAvailabilityResponse(
        stay=_summary(stay), hotel=HotelAvailabilityOut.model_validate(result)
    )


@router.get("/availability", response_model=CityAvailabilityResponse)
def city_availability(
    query: Annotated[CityAvailabilityQuery, Query()],
    db: Session = Depends(get_db),
) -> CityAvailabilityResponse:
    """Hotels in a city with at least one free room suitable for the stay."""
    hotels = availability.city_availability(
        db, query.city, query.check_in, query.check_out, query.guests
    )
    return CityAvailabilityResponse(
        stay=_summary(query),
        city=query.city,
        hotels=[HotelAvailabilityOut.model_validate(h) for h in hotels],
    )

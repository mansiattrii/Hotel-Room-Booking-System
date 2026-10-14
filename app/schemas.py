from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    model_validator,
)

from app.config import get_settings


def _lowercase(value: str) -> str:
    return value.lower()


Email = Annotated[EmailStr, AfterValidator(_lowercase)]
# Upper bound stops someone submitting megabytes to be hashed.
Password = Annotated[str, Field(min_length=8, max_length=128)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InputModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class RegisterRequest(BaseModel):
    email: Email
    password: Password


class LoginRequest(BaseModel):
    email: Email
    password: Annotated[str, Field(max_length=128)]


class UserOut(ORMModel):
    id: int
    email: str
    role: Literal["user", "admin"]
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Lifetime in seconds")


Money = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]


class HotelCreate(InputModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    city: Annotated[str, Field(min_length=1, max_length=100)]
    address: Annotated[str, Field(min_length=1, max_length=300)]
    star_rating: Annotated[int, Field(ge=1, le=5)]


class HotelOut(ORMModel):
    id: int
    name: str
    city: str
    address: str
    star_rating: int


class RoomTypeCreate(InputModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    capacity: Annotated[int, Field(ge=1, le=20)]
    base_price_per_night: Money


class RoomTypeOut(ORMModel):
    id: int
    hotel_id: int
    name: str
    capacity: int
    base_price_per_night: Decimal


class HotelDetail(HotelOut):
    room_types: list[RoomTypeOut]


class RoomCreate(InputModel):
    room_type_id: int
    room_number: Annotated[str, Field(min_length=1, max_length=20)]


class RoomOut(ORMModel):
    id: int
    hotel_id: int
    room_type_id: int
    room_number: str


class StayRequest(BaseModel):
    """Dates and party size shared by availability searches and bookings."""

    check_in: date
    check_out: date
    guests: Annotated[int, Field(ge=1, le=20)]

    @model_validator(mode="after")
    def _validate_stay(self) -> Self:
        settings = get_settings()
        today = datetime.now(UTC).date()
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if self.check_in < today:
            raise ValueError("check_in cannot be in the past")
        if (self.check_out - self.check_in).days > settings.max_stay_nights:
            raise ValueError(f"stay cannot be longer than {settings.max_stay_nights} nights")
        if self.check_in > today + timedelta(days=settings.booking_horizon_days):
            raise ValueError(
                f"check_in cannot be more than {settings.booking_horizon_days} days ahead"
            )
        return self

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


class CityAvailabilityQuery(StayRequest):
    city: Annotated[str, Field(min_length=1, max_length=100)]


class StaySummary(BaseModel):
    check_in: date
    check_out: date
    nights: int
    guests: int


class RoomTypeAvailabilityOut(ORMModel):
    room_type_id: int
    name: str
    capacity: int
    price_per_night: Decimal
    total_price: Decimal
    room_ids: list[int]

    @computed_field
    @property
    def available_rooms(self) -> int:
        return len(self.room_ids)


class HotelAvailabilityOut(ORMModel):
    hotel_id: int
    name: str
    city: str
    address: str
    star_rating: int
    room_types: list[RoomTypeAvailabilityOut]


class HotelAvailabilityResponse(BaseModel):
    stay: StaySummary
    hotel: HotelAvailabilityOut


class CityAvailabilityResponse(BaseModel):
    stay: StaySummary
    city: str
    hotels: list[HotelAvailabilityOut]


class BookingCreate(StayRequest):
    room_id: int


class BookingOut(ORMModel):
    id: int
    user_id: int
    room_id: int
    check_in: date
    check_out: date
    guests: int
    status: Literal["held", "confirmed", "cancelled", "expired"]
    hold_expires_at: datetime | None
    total_price: Decimal
    created_at: datetime
    updated_at: datetime


class ConfirmRequest(BaseModel):
    payment_token: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            description="Mock provider token. 'tok_declined' is declined; anything else succeeds.",
            examples=["tok_visa"],
        ),
    ]

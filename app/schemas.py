from datetime import datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
)


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

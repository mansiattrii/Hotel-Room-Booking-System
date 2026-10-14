"""ORM models.

The Alembic migrations are the source of truth for the schema. These models mirror
them (including constraints) so the code documents what the database enforces.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

USER_ROLES = ("user", "admin")
BOOKING_STATUSES = ("held", "confirmed", "cancelled", "expired")
ACTIVE_BOOKING_STATUSES = ("held", "confirmed")
PAYMENT_STATUSES = ("succeeded", "failed")

Money = Numeric(10, 2)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_in_list("role", USER_ROLES), name="role_valid"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Hotel(Base):
    __tablename__ = "hotels"
    __table_args__ = (CheckConstraint("star_rating BETWEEN 1 AND 5", name="star_rating_range"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(300))
    star_rating: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoomType(Base):
    __tablename__ = "room_types"
    __table_args__ = (
        UniqueConstraint("hotel_id", "name"),
        # Target for the composite FK from rooms; id alone is already unique.
        UniqueConstraint("hotel_id", "id"),
        CheckConstraint("capacity > 0", name="capacity_positive"),
        CheckConstraint("base_price_per_night > 0", name="price_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id"))
    name: Mapped[str] = mapped_column(String(100))
    capacity: Mapped[int] = mapped_column(SmallInteger)
    base_price_per_night: Mapped[Decimal] = mapped_column(Money)


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        UniqueConstraint("hotel_id", "room_number"),
        # Guarantees the room type belongs to the same hotel as the room.
        ForeignKeyConstraint(
            ["hotel_id", "room_type_id"],
            ["room_types.hotel_id", "room_types.id"],
            name="fk_rooms_hotel_id_room_type_id_room_types",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id"))
    room_type_id: Mapped[int] = mapped_column(BigInteger)
    room_number: Mapped[str] = mapped_column(String(20))


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("check_out > check_in", name="dates_ordered"),
        CheckConstraint(_in_list("status", BOOKING_STATUSES), name="status_valid"),
        CheckConstraint("status <> 'held' OR hold_expires_at IS NOT NULL", name="hold_has_expiry"),
        CheckConstraint("guests > 0", name="guests_positive"),
        CheckConstraint("total_price >= 0", name="total_price_non_negative"),
        UniqueConstraint("user_id", "idempotency_key"),
        ExcludeConstraint(
            ("room_id", "="),
            (text("daterange(check_in, check_out, '[)')"), "&&"),
            name="ex_bookings_no_overlap",
            using="gist",
            where=text(_in_list("status", ACTIVE_BOOKING_STATUSES)),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"))
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    guests: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(String(16))
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_price: Mapped[Decimal] = mapped_column(Money)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(_in_list("status", PAYMENT_STATUSES), name="status_valid"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), unique=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(16))
    provider_ref: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

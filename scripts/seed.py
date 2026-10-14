"""Generate a realistic dataset for development and the search benchmark.

    python -m scripts.seed                       # 200 hotels, 10,000 rooms, 200,000 bookings
    python -m scripts.seed --reset               # wipe all existing data first
    python -m scripts.seed --hotels 20 --rooms 1000 --bookings 10000 --users 500

The random seed is fixed, so the same arguments produce the same hotels, rooms and booking
pattern. Booking dates are laid out relative to today so there is always future inventory
to search.
"""

import argparse
import logging
import random
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal

from sqlalchemy import Connection, insert, text

from app.config import get_settings
from app.db import build_engine
from app.logging_config import configure_logging
from app.models import Hotel, Room, RoomType, User
from app.security import hash_password

logger = logging.getLogger("scripts.seed")

CITIES = [
    "Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Chennai", "Kolkata", "Pune", "Jaipur",
    "Ahmedabad", "Goa", "Kochi", "Udaipur", "Chandigarh", "Lucknow", "Mysuru", "Shimla",
    "Varanasi", "Indore", "Amritsar", "Rishikesh", "Agra", "Bhopal", "Coimbatore", "Nagpur",
]  # fmt: skip

NAME_PREFIXES = [
    "Grand", "Royal", "Imperial", "Silver", "Golden", "Blue", "Palm", "Lotus", "Heritage",
    "Park", "Lake", "Hill", "Garden", "Harbour", "City", "Sunrise", "Orchid", "Maple",
]  # fmt: skip
NAME_SUFFIXES = ["Residency", "Inn", "Palace", "Suites", "Retreat", "Plaza", "Regency", "Stay"]
STREETS = ["MG Road", "Station Road", "Mall Road", "Ring Road", "Lake View Road", "Main Street"]

# (name, capacity, price multiplier relative to a hotel's base nightly rate)
ROOM_TYPE_CATALOG = [
    ("Single", 1, 0.6),
    ("Standard", 2, 1.0),
    ("Twin", 2, 1.05),
    ("Deluxe", 2, 1.4),
    ("Family", 4, 1.8),
    ("Suite", 4, 2.6),
]
BASE_RATE_BY_STARS = {1: 1200, 2: 2000, 3: 3500, 4: 6000, 5: 11000}
MAX_STAY_NIGHTS = 7

BOOKING_COLUMNS = (
    "user_id", "room_id", "check_in", "check_out", "guests", "status",
    "hold_expires_at", "total_price", "idempotency_key", "created_at", "updated_at",
)  # fmt: skip


@dataclass(frozen=True)
class SeedConfig:
    hotels: int = 200
    cities: int = 20
    rooms: int = 10_000
    bookings: int = 200_000
    users: int = 5_000
    history_days: int = 365
    future_days: int = 180
    random_seed: int = 42
    user_password: str = "password123"

    def validate(self) -> None:
        """Reject combinations that can't be laid out without overlapping bookings."""
        if not 1 <= self.cities <= len(CITIES):
            raise ValueError(f"--cities must be between 1 and {len(CITIES)}")
        if self.rooms < self.hotels:
            raise ValueError("--rooms must be at least --hotels")
        window = self.history_days + self.future_days
        # _split gives a room up to 1.5x the average, and each booking needs at least a night.
        busiest_room = 1.5 * self.bookings / self.rooms + 1
        if busiest_room > window:
            raise ValueError(
                f"about {busiest_room:.0f} bookings per room do not fit in a {window}-day "
                "window; add rooms or reduce bookings"
            )


@dataclass(frozen=True)
class RoomInfo:
    id: int
    capacity: int
    price: Decimal


def seed(conn: Connection, cfg: SeedConfig, today: date) -> dict[str, int]:
    """Insert the whole dataset using the given connection; the caller owns the transaction."""
    cfg.validate()
    rng = random.Random(cfg.random_seed)

    user_ids = _insert_users(conn, cfg)
    hotels = _insert_hotels(conn, rng, cfg)
    rooms = _insert_rooms(conn, rng, cfg, hotels)
    booking_count = _copy_bookings(conn, rng, cfg, rooms, user_ids, today)
    payment_count = _insert_payments(conn)

    return {
        "users": len(user_ids),
        "hotels": len(hotels),
        "rooms": len(rooms),
        "bookings": booking_count,
        "payments": payment_count,
    }


def _insert_users(conn: Connection, cfg: SeedConfig) -> list[int]:
    # Argon2 is deliberately slow; hashing once and sharing it keeps seeding fast.
    password_hash = hash_password(cfg.user_password)
    rows = [
        {"email": f"user{n}@example.com", "password_hash": password_hash}
        for n in range(1, cfg.users + 1)
    ]
    return list(conn.scalars(insert(User).returning(User.id), rows))


def _insert_hotels(conn: Connection, rng: random.Random, cfg: SeedConfig) -> list[tuple[int, int]]:
    """Insert hotels. Returns (hotel_id, star_rating) pairs."""
    cities = CITIES[: cfg.cities]
    # Larger cities get more hotels, which makes some searches heavier than others.
    city_weights = [1 / (rank + 1) ** 0.7 for rank in range(len(cities))]
    rows = []
    for n in range(cfg.hotels):
        city = rng.choices(cities, weights=city_weights)[0]
        rows.append(
            {
                "name": f"{rng.choice(NAME_PREFIXES)} {rng.choice(NAME_SUFFIXES)} {n + 1}",
                "city": city,
                "address": f"{rng.randint(1, 400)} {rng.choice(STREETS)}, {city}",
                "star_rating": rng.choices([1, 2, 3, 4, 5], weights=[1, 3, 5, 4, 2])[0],
            }
        )
    result = conn.execute(
        insert(Hotel).returning(Hotel.id, Hotel.star_rating, sort_by_parameter_order=True),
        rows,
    )
    return [(row.id, row.star_rating) for row in result]


def _insert_rooms(
    conn: Connection,
    rng: random.Random,
    cfg: SeedConfig,
    hotels: Sequence[tuple[int, int]],
) -> list[RoomInfo]:
    room_counts = _split(cfg.rooms, len(hotels), rng)
    rooms: list[RoomInfo] = []

    for (hotel_id, stars), room_count in zip(hotels, room_counts, strict=True):
        catalog = sorted(rng.sample(ROOM_TYPE_CATALOG, rng.randint(3, 5)), key=lambda t: t[2])
        type_rows = [
            {
                "hotel_id": hotel_id,
                "name": name,
                "capacity": capacity,
                "base_price_per_night": _price(BASE_RATE_BY_STARS[stars] * mult, rng),
            }
            for name, capacity, mult in catalog
        ]
        types = conn.execute(
            insert(RoomType).returning(
                RoomType.id,
                RoomType.capacity,
                RoomType.base_price_per_night,
                sort_by_parameter_order=True,
            ),
            type_rows,
        ).all()

        if room_count == 0:
            continue
        # Cheaper types are more common, as in real hotels.
        type_weights = [len(types) - i for i in range(len(types))]
        assigned = rng.choices(types, weights=type_weights, k=room_count)
        room_rows = [
            {
                "hotel_id": hotel_id,
                "room_type_id": rt.id,
                "room_number": f"{i // 20 + 1}{i % 20 + 1:02d}",
            }
            for i, rt in enumerate(assigned)
        ]
        room_ids = conn.scalars(
            insert(Room).returning(Room.id, sort_by_parameter_order=True), room_rows
        ).all()
        rooms.extend(
            RoomInfo(room_id, rt.capacity, rt.base_price_per_night)
            for room_id, rt in zip(room_ids, assigned, strict=True)
        )
    return rooms


def _copy_bookings(
    conn: Connection,
    rng: random.Random,
    cfg: SeedConfig,
    rooms: Sequence[RoomInfo],
    user_ids: Sequence[int],
    today: date,
) -> int:
    """Stream bookings into PostgreSQL with COPY, the fastest bulk-load path."""
    raw = conn.connection.driver_connection
    columns = ", ".join(BOOKING_COLUMNS)
    rows = _booking_rows(rng, cfg, rooms, user_ids, today)
    count = 0
    with raw.cursor() as cur, cur.copy(f"COPY bookings ({columns}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
            count += 1
    return count


def _booking_rows(
    rng: random.Random,
    cfg: SeedConfig,
    rooms: Sequence[RoomInfo],
    user_ids: Sequence[int],
    today: date,
) -> Iterator[tuple[object, ...]]:
    """Yield non-overlapping bookings for every room, spread over the date window."""
    window_start = today - timedelta(days=cfg.history_days)
    window_days = cfg.history_days + cfg.future_days
    per_room = _split(cfg.bookings, len(rooms), rng)
    key = 0

    for room, count in zip(rooms, per_room, strict=True):
        if count == 0:
            continue
        stays = [rng.randint(1, MAX_STAY_NIGHTS) for _ in range(count)]
        if sum(stays) > window_days:
            stays = [1] * count
        gaps = _split(window_days - sum(stays), count + 1, rng)

        cursor = window_start + timedelta(days=gaps[0])
        for stay, gap_after in zip(stays, gaps[1:], strict=True):
            check_in, check_out = cursor, cursor + timedelta(days=stay)
            cursor = check_out + timedelta(days=gap_after)
            key += 1

            created_at = datetime.combine(
                check_in - timedelta(days=rng.randint(1, 90)), dtime(12), UTC
            ) + timedelta(minutes=rng.randint(0, 600))
            status = _booking_status(rng, check_in, today)
            hold_expires_at = created_at + timedelta(minutes=10) if status == "expired" else None
            yield (
                rng.choice(user_ids),
                room.id,
                check_in,
                check_out,
                rng.randint(1, room.capacity),
                status,
                hold_expires_at,
                room.price * stay,
                f"seed-{key}",
                created_at,
                created_at,
            )


def _booking_status(rng: random.Random, check_in: date, today: date) -> str:
    if check_in < today:
        return rng.choices(["confirmed", "cancelled", "expired"], weights=[85, 12, 3])[0]
    return rng.choices(["confirmed", "cancelled"], weights=[85, 15])[0]


def _insert_payments(conn: Connection) -> int:
    """One successful payment per confirmed booking, done as a single set-based INSERT."""
    result = conn.execute(
        text(
            "INSERT INTO payments (booking_id, amount, status, provider_ref, created_at) "
            "SELECT id, total_price, 'succeeded', 'seed-' || id, "
            "       created_at + interval '5 minutes' "
            "FROM bookings WHERE status = 'confirmed'"
        )
    )
    return result.rowcount


def _split(total: int, parts: int, rng: random.Random) -> list[int]:
    """Split `total` into `parts` non-negative integers that vary randomly and sum exactly."""
    weights = [rng.uniform(0.5, 1.5) for _ in range(parts)]
    scale = total / sum(weights)
    counts = [int(w * scale) for w in weights]
    for i in rng.sample(range(parts), total - sum(counts)):
        counts[i] += 1
    return counts


def _price(amount: float, rng: random.Random) -> Decimal:
    """Round to the nearest 50, as hotels usually price."""
    varied = amount * rng.uniform(0.9, 1.1)
    return Decimal(round(varied / 50) * 50).quantize(Decimal("0.01"))


def _truncate_all(conn: Connection) -> None:
    conn.execute(
        text(
            "TRUNCATE payments, bookings, rooms, room_types, hotels, users RESTART IDENTITY CASCADE"
        )
    )


def _parse_args() -> argparse.Namespace:
    defaults = SeedConfig()
    parser = argparse.ArgumentParser(description="Seed the hotel booking database.")
    parser.add_argument("--hotels", type=int, default=defaults.hotels)
    parser.add_argument("--cities", type=int, default=defaults.cities)
    parser.add_argument("--rooms", type=int, default=defaults.rooms)
    parser.add_argument("--bookings", type=int, default=defaults.bookings)
    parser.add_argument("--users", type=int, default=defaults.users)
    parser.add_argument("--random-seed", type=int, default=defaults.random_seed)
    parser.add_argument(
        "--database-url", help="Defaults to DATABASE_URL. Use to seed another database."
    )
    parser.add_argument(
        "--reset", action="store_true", help="Delete ALL existing data (including admins) first."
    )
    return parser.parse_args()


def main() -> int:
    """Entry point; returns a process exit code."""
    args = _parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    cfg = SeedConfig(
        hotels=args.hotels,
        cities=args.cities,
        rooms=args.rooms,
        bookings=args.bookings,
        users=args.users,
        random_seed=args.random_seed,
    )
    try:
        cfg.validate()
    except ValueError as exc:
        logger.error("invalid seed configuration", extra={"error": str(exc)})
        return 2

    engine = build_engine(args.database_url or settings.database_url)
    started = time.perf_counter()
    with engine.begin() as conn:
        if args.reset:
            _truncate_all(conn)
        elif conn.scalar(text("SELECT EXISTS (SELECT 1 FROM hotels)")):
            logger.error("database already has data; rerun with --reset to replace it")
            return 1
        counts = seed(conn, cfg, today=date.today())

    # Fresh planner statistics, so the first queries and EXPLAIN plans reflect the new data.
    with engine.connect() as conn:
        conn.execute(text("ANALYZE"))
        conn.commit()
    engine.dispose()

    logger.info(
        "seed complete", extra={**counts, "seconds": round(time.perf_counter() - started, 1)}
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

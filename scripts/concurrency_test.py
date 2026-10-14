"""Concurrency experiment: N users try to book the same room for the same dates at once.

    python -m scripts.concurrency_test                  # 100 requests, 5 runs per scenario
    python -m scripts.concurrency_test --requests 100 --runs 5

Each scenario starts a real API server (uvicorn subprocess) with BOOKING_STRATEGY set, so
every request goes through HTTP, JWT auth, the booking service and PostgreSQL exactly as in
normal use. The requests are released together once all client connections are open.
After each burst an SQL query counts overlapping active bookings, the ground truth for
whether the room was double-booked.

TEST DATABASE ONLY. The script truncates tables and drops the exclusion constraint for the
scenarios that judge a strategy on its own; it restores the constraint when it finishes.
Results: results/concurrency.csv (one row per run) and results/concurrency.md.
"""

import argparse
import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median

import httpx
from sqlalchemy import Connection, Engine, text

from app.config import get_settings
from app.db import build_engine
from app.logging_config import configure_logging
from app.security import create_access_token
from app.services.locking import Strategy
from scripts import exclusion_toggle
from scripts.experiment_utils import (
    RESULTS_DIR,
    ROOT,
    ExperimentSetupError,
    environment,
    experiment_database_url,
    markdown_table,
    migrate,
    percentile,
    write_csv,
)

logger = logging.getLogger("scripts.concurrency_test")

API = "/api/v1"
STAY_OFFSET_DAYS, STAY_NIGHTS = 30, 3
SERVER_START_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Scenario:
    strategy: Strategy
    constraint: bool

    @property
    def label(self) -> str:
        return f"{self.strategy} ({'with' if self.constraint else 'without'} constraint)"


SCENARIOS = (
    Scenario(Strategy.NAIVE, constraint=False),
    Scenario(Strategy.NAIVE, constraint=True),
    Scenario(Strategy.PESSIMISTIC, constraint=False),
    Scenario(Strategy.CONSTRAINT, constraint=True),
)


@dataclass(frozen=True)
class Outcome:
    status: int | None
    error_code: str | None
    seconds: float


@dataclass
class RunResult:
    scenario: Scenario
    run: int
    outcomes: list[Outcome]
    total_seconds: float
    active_bookings: int
    overlapping_pairs: int
    other_errors: Counter[str] = field(init=False)

    def __post_init__(self) -> None:
        self.other_errors = Counter(
            f"{o.status or 'no response'} {o.error_code or ''}".strip()
            for o in self.outcomes
            if o.status not in (201, 409)
        )

    @property
    def booked(self) -> int:
        return sum(o.status == 201 for o in self.outcomes)

    @property
    def conflicts(self) -> int:
        return sum(o.status == 409 for o in self.outcomes)

    def latency_ms(self, pct: float) -> float:
        return percentile([o.seconds for o in self.outcomes], pct) * 1000


@dataclass(frozen=True)
class Fixtures:
    room_id: int
    tokens: list[str]


def prepare_fixtures(conn: Connection, requests: int) -> Fixtures:
    """One hotel with one room, and one user (with a token) per request."""
    conn.execute(
        text(
            "TRUNCATE payments, bookings, rooms, room_types, hotels, users RESTART IDENTITY CASCADE"
        )
    )
    hotel_id = conn.scalar(
        text(
            "INSERT INTO hotels (name, city, address, star_rating) "
            "VALUES ('Experiment Hotel', 'Pune', 'Experiment Road', 3) RETURNING id"
        )
    )
    room_type_id = conn.scalar(
        text(
            "INSERT INTO room_types (hotel_id, name, capacity, base_price_per_night) "
            "VALUES (:h, 'Standard', 2, 2000) RETURNING id"
        ),
        {"h": hotel_id},
    )
    room_id = conn.scalar(
        text(
            "INSERT INTO rooms (hotel_id, room_type_id, room_number) "
            "VALUES (:h, :rt, '101') RETURNING id"
        ),
        {"h": hotel_id, "rt": room_type_id},
    )
    # The users never log in (tokens are issued directly), so the hash is a placeholder.
    user_ids = conn.scalars(
        text(
            "INSERT INTO users (email, password_hash) "
            "SELECT 'racer' || g || '@example.com', 'not-a-hash' "
            "FROM generate_series(1, :n) g RETURNING id"
        ),
        {"n": requests},
    ).all()
    tokens = [create_access_token(user_id, timedelta(hours=1)) for user_id in user_ids]
    return Fixtures(room_id=room_id, tokens=tokens)


def _clear_bookings(conn: Connection) -> None:
    conn.execute(text("TRUNCATE payments, bookings"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def api_server(database_url: str, strategy: Strategy) -> Iterator[str]:
    """Run the API in a subprocess with the given strategy; yield its base URL."""
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "BOOKING_STRATEGY": str(strategy),
        "LOG_LEVEL": "WARNING",
    }
    cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", str(port),
        "--log-level", "warning", "--no-access-log",
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_healthy(base_url, proc)
        yield base_url
    finally:
        _stop(proc)


def _stop(proc: subprocess.Popen[bytes]) -> None:
    # On Windows a venv's python.exe is a launcher that starts the real interpreter as a
    # child; terminate() would kill only the launcher and leave the server running.
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    else:
        proc.terminate()
    proc.wait(timeout=10)


def _wait_until_healthy(base_url: str, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ExperimentSetupError(f"API server exited with code {proc.returncode}")
        try:
            if httpx.get(f"{base_url}{API}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise ExperimentSetupError("API server did not become healthy in time")


async def burst(base_url: str, fixtures: Fixtures, run_tag: str) -> tuple[list[Outcome], float]:
    """Send one booking request per user, all released at the same moment."""
    today = datetime.now(UTC).date()
    body = {
        "room_id": fixtures.room_id,
        "check_in": (today + timedelta(days=STAY_OFFSET_DAYS)).isoformat(),
        "check_out": (today + timedelta(days=STAY_OFFSET_DAYS + STAY_NIGHTS)).isoformat(),
        "guests": 1,
    }
    n = len(fixtures.tokens)
    limits = httpx.Limits(max_connections=n, max_keepalive_connections=n)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=60) as client:
        # Open the client connections (and warm the server's DB pool) before the start.
        await asyncio.gather(*(client.get(f"{API}/health") for _ in range(n)))
        start = asyncio.Event()

        async def attempt(i: int, token: str) -> Outcome:
            await start.wait()
            began = time.perf_counter()
            headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": f"{run_tag}-{i}"}
            try:
                response = await client.post(f"{API}/bookings", json=body, headers=headers)
            except httpx.HTTPError as exc:
                return Outcome(None, type(exc).__name__, time.perf_counter() - began)
            code = None
            if response.status_code >= 400:
                code = response.json().get("error", {}).get("code")
            return Outcome(response.status_code, code, time.perf_counter() - began)

        tasks = [asyncio.create_task(attempt(i, t)) for i, t in enumerate(fixtures.tokens)]
        await asyncio.sleep(0)  # let every task reach start.wait()
        began = time.perf_counter()
        start.set()
        outcomes = await asyncio.gather(*tasks)
        return list(outcomes), time.perf_counter() - began


def _count_active(conn: Connection, room_id: int) -> int:
    return int(
        conn.scalar(
            text(
                "SELECT count(*) FROM bookings "
                "WHERE room_id = :r AND status IN ('held', 'confirmed')"
            ),
            {"r": room_id},
        )
    )


def run_scenario(
    engine: Engine, database_url: str, scenario: Scenario, fixtures: Fixtures, runs: int
) -> list[RunResult]:
    with engine.begin() as conn:
        _clear_bookings(conn)
        if scenario.constraint:
            exclusion_toggle.enable(conn)
        else:
            exclusion_toggle.disable(conn)

    results = []
    with api_server(database_url, scenario.strategy) as base_url:
        for run in range(1, runs + 1):
            with engine.begin() as conn:
                _clear_bookings(conn)
            tag = f"{scenario.strategy}-{scenario.constraint}-{run}"
            outcomes, total = asyncio.run(burst(base_url, fixtures, tag))
            with engine.connect() as conn:
                result = RunResult(
                    scenario=scenario,
                    run=run,
                    outcomes=outcomes,
                    total_seconds=total,
                    active_bookings=_count_active(conn, fixtures.room_id),
                    overlapping_pairs=exclusion_toggle.count_overlapping_active_pairs(conn),
                )
            results.append(result)
            logger.info(
                "run finished",
                extra={
                    "scenario": scenario.label,
                    "run": run,
                    "booked": result.booked,
                    "conflicts": result.conflicts,
                    "other": sum(result.other_errors.values()),
                    "overlapping_pairs": result.overlapping_pairs,
                    "seconds": round(total, 3),
                },
            )
    return results


CSV_HEADERS = (
    "scenario", "strategy", "exclusion_constraint", "run", "requests", "booked_201",
    "conflict_409", "other_errors", "other_error_detail", "total_seconds",
    "latency_p50_ms", "latency_p95_ms", "active_bookings_in_db", "overlapping_pairs",
)  # fmt: skip


def _csv_row(r: RunResult) -> list[object]:
    return [
        r.scenario.label,
        r.scenario.strategy,
        r.scenario.constraint,
        r.run,
        len(r.outcomes),
        r.booked,
        r.conflicts,
        sum(r.other_errors.values()),
        "; ".join(f"{k} x{v}" for k, v in sorted(r.other_errors.items())),
        f"{r.total_seconds:.3f}",
        f"{r.latency_ms(50):.1f}",
        f"{r.latency_ms(95):.1f}",
        r.active_bookings,
        r.overlapping_pairs,
    ]


def _span(values: list[int]) -> str:
    low, high = min(values), max(values)
    return str(low) if low == high else f"{low} to {high}"


def _summary_row(scenario: Scenario, runs: list[RunResult]) -> list[object]:
    double_booked = sum(r.overlapping_pairs > 0 for r in runs)
    return [
        scenario.label,
        len(runs),
        _span([r.booked for r in runs]),
        _span([r.conflicts for r in runs]),
        _span([sum(r.other_errors.values()) for r in runs]),
        _span([r.active_bookings for r in runs]),
        _span([r.overlapping_pairs for r in runs]),
        f"{double_booked} of {len(runs)}",
        f"{median(r.total_seconds for r in runs):.3f}",
        f"{median(r.latency_ms(95) for r in runs):.1f}",
    ]


def write_report(results: list[RunResult], env: dict[str, str], requests: int) -> None:
    settings = get_settings()
    write_csv(RESULTS_DIR / "concurrency.csv", CSV_HEADERS, [_csv_row(r) for r in results])

    by_scenario = [(s, [r for r in results if r.scenario == s]) for s in SCENARIOS]
    summary = markdown_table(
        [
            "Scenario", "Runs", "201 booked", "409 conflict", "Other errors",
            "Active bookings in DB", "Overlapping pairs", "Runs that double-booked",
            "Total time, median (s)", "p95 latency, median (ms)",
        ],
        [_summary_row(s, runs) for s, runs in by_scenario if runs],
    )  # fmt: skip
    per_run = markdown_table(
        ["Scenario", "Run", "201", "409", "Other", "Overlapping pairs", "Total (s)", "p95 (ms)"],
        [
            [
                r.scenario.label, r.run, r.booked, r.conflicts,
                sum(r.other_errors.values()), r.overlapping_pairs,
                f"{r.total_seconds:.3f}", f"{r.latency_ms(95):.1f}",
            ]
            for r in results
        ],
    )  # fmt: skip
    other = sorted({k for r in results for k in r.other_errors})
    env_lines = "\n".join(f"- {k}: {v}" for k, v in env.items())

    report = f"""# Concurrency experiment

Generated by `python -m scripts.concurrency_test`. Every number below comes from that run;
raw per-run rows are in `concurrency.csv`.

## Setup

- {requests} users each send one `POST /api/v1/bookings` for the same room and the same
  {STAY_NIGHTS} nights, released at the same instant from one asyncio client after all
  {requests} HTTP connections were opened.
- One API process (uvicorn, sync endpoints on its thread pool), DB pool size
  {settings.db_pool_size} + overflow {settings.db_max_overflow}, isolation READ COMMITTED.
- "without constraint": `ex_bookings_no_overlap` dropped on the test database, so the strategy
  alone decides. "with constraint": the production schema.
- "Overlapping pairs" is counted in SQL after each run: pairs of held/confirmed bookings of the
  room whose date ranges overlap. Anything above 0 is a double booking.
- Total time: from releasing the requests until the last response arrived.
{env_lines}

## Summary

{summary}

Other error responses seen: {", ".join(other) if other else "none"}.

## Every run

{per_run}
"""
    (RESULTS_DIR / "concurrency.md").write_text(report, encoding="utf-8")


def main() -> int:
    """Entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(description="Run the double-booking concurrency experiment.")
    parser.add_argument("--requests", type=int, default=100, help="Concurrent requests per run.")
    parser.add_argument("--runs", type=int, default=5, help="Runs per scenario.")
    args = parser.parse_args()

    configure_logging(get_settings().log_level)
    # httpx logs every request at INFO; that would add log I/O inside the timed burst.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        database_url = experiment_database_url()
    except ExperimentSetupError as exc:
        logger.error(str(exc))
        return 2

    engine = build_engine(database_url)
    try:
        with engine.connect() as conn:
            exclusion_toggle.ensure_test_database(conn)
            env = environment(conn)
    except exclusion_toggle.UnsafeDatabaseError as exc:
        logger.error(str(exc))
        engine.dispose()
        return 1

    try:
        migrate(database_url, "head")
        with engine.begin() as conn:
            fixtures = prepare_fixtures(conn, args.requests)

        results: list[RunResult] = []
        for scenario in SCENARIOS:
            results += run_scenario(engine, database_url, scenario, fixtures, args.runs)
        write_report(results, env, args.requests)
    except ExperimentSetupError as exc:
        logger.error(str(exc))
        return 1
    finally:
        with engine.begin() as conn:
            _clear_bookings(conn)
            exclusion_toggle.enable(conn)
        engine.dispose()

    logger.info("results written", extra={"directory": str(RESULTS_DIR)})
    return 0


if __name__ == "__main__":
    sys.exit(main())

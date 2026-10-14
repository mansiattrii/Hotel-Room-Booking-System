"""Search benchmark: query latency with and without the indexes the queries rely on.

    python -m scripts.benchmark_search                    # 100 timed executions per query
    python -m scripts.benchmark_search --iterations 300

Seeds the test database with the full dataset (scripts.seed defaults, fixed random seed),
then times each query against the same fixed list of parameters in three index states:

none        ex_bookings_no_overlap dropped (taking its GiST index with it) and migration
            0003 downgraded (dropping ix_bookings_held_expiry). Nothing indexes bookings.
btree       as "none", plus a plain B-tree on bookings(room_id): the obvious index someone
            would add without the exclusion constraint.
production  the production schema: GiST index of the exclusion constraint, plus 0003.

Timings are client-side wall time for execute + fetch over one pooled connection, so they
include the round trip to PostgreSQL. One EXPLAIN (ANALYZE, BUFFERS) per query and state is
saved to results/explain/. Summary: results/search_benchmark.md and .csv.

TEST DATABASE ONLY: it truncates and reseeds, drops a constraint and downgrades a migration,
and puts the production schema back at the end.
"""

import argparse
import logging
import random
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Connection, Engine, TextClause, text

from app.config import get_settings
from app.db import build_engine
from app.logging_config import configure_logging
from app.services.availability import BY_CITY_QUERY, BY_HOTEL_QUERY
from app.services.booking import EXPIRE_LAPSED_HOLDS
from scripts import exclusion_toggle
from scripts.experiment_utils import (
    RESULTS_DIR,
    ExperimentSetupError,
    environment,
    experiment_database_url,
    markdown_table,
    migrate,
    percentile,
    write_csv,
)
from scripts.seed import SeedConfig
from scripts.seed import seed as seed_dataset

logger = logging.getLogger("scripts.benchmark_search")

PARAMS_SEED = 7
REVISION_WITHOUT_QUERY_INDEXES = "0002"
BTREE_INDEX = "ix_benchmark_bookings_room_id"
HELD_EXPIRY_INDEX = "ix_bookings_held_expiry"


@dataclass(frozen=True)
class IndexState:
    name: str
    description: str


NONE = IndexState("none", "no index on bookings (GiST constraint dropped, 0003 downgraded)")
BTREE = IndexState("btree", "B-tree on bookings(room_id) only (GiST dropped, 0003 downgraded)")
PRODUCTION = IndexState("production", "production schema (GiST exclusion index + 0003)")
STATES = (NONE, BTREE, PRODUCTION)


@dataclass(frozen=True)
class Workload:
    name: str
    description: str
    statement: TextClause
    params: list[dict[str, object]]


@dataclass(frozen=True)
class Measurement:
    state: IndexState
    workload: str
    timings_ms: list[float]
    explain_ms: float

    @property
    def p50(self) -> float:
        return percentile(self.timings_ms, 50)

    @property
    def p95(self) -> float:
        return percentile(self.timings_ms, 95)


def build_workloads(conn: Connection, iterations: int, today: date) -> list[Workload]:
    """Fixed, seeded parameter lists, so every state runs exactly the same queries."""
    rng = random.Random(PARAMS_SEED)
    cities = conn.scalars(text("SELECT DISTINCT city FROM hotels ORDER BY city")).all()
    hotel_ids = conn.scalars(text("SELECT id FROM hotels ORDER BY id")).all()

    def stay() -> dict[str, object]:
        check_in = today + timedelta(days=rng.randint(1, 150))
        nights = rng.randint(1, 7)
        return {
            "check_in": check_in,
            "check_out": check_in + timedelta(days=nights),
            "guests": rng.randint(1, 4),
            "nights": nights,
        }

    return [
        Workload(
            "city_search",
            "`GET /availability?city=`: every hotel in a city with a suitable free room",
            BY_CITY_QUERY,
            [{**stay(), "city": rng.choice(cities)} for _ in range(iterations)],
        ),
        Workload(
            "hotel_search",
            "`GET /hotels/{id}/availability`: free rooms of one hotel",
            BY_HOTEL_QUERY,
            [{**stay(), "hotel_id": rng.choice(hotel_ids)} for _ in range(iterations)],
        ),
        Workload(
            "worker_sweep",
            "worker: expire lapsed holds (the real UPDATE, rolled back after each execution)",
            EXPIRE_LAPSED_HOLDS,
            [{"batch_size": 1000} for _ in range(iterations)],
        ),
    ]


def _execute(conn: Connection, statement: TextClause, params: dict[str, object]) -> None:
    result = conn.execute(statement, params)
    if result.returns_rows:
        result.all()


def time_workload(conn: Connection, workload: Workload, warmup: int) -> list[float]:
    """Execute every parameter set once, each in its own transaction, rolled back."""
    for params in workload.params[:warmup]:
        _execute(conn, workload.statement, params)
        conn.rollback()
    timings = []
    for params in workload.params:
        started = time.perf_counter()
        _execute(conn, workload.statement, params)
        timings.append((time.perf_counter() - started) * 1000)
        conn.rollback()
    return timings


def explain(conn: Connection, statement: TextClause, params: dict[str, object]) -> str:
    """EXPLAIN ANALYZE output for a statement, with the same typed parameters the app binds."""
    # Reusing the statement's own bind parameters matters: an id bound as INTEGER instead of
    # BIGINT produces a different (much worse) plan; see NOTES.md, Milestone 3.
    explained = text(f"EXPLAIN (ANALYZE, BUFFERS) {statement.text}").bindparams(
        *statement._bindparams.values()
    )
    lines = conn.execute(explained, params).scalars().all()
    conn.rollback()
    return "\n".join(lines)


def _execution_ms(plan: str) -> float:
    match = re.search(r"Execution Time: ([\d.]+) ms", plan)
    if match is None:
        raise ExperimentSetupError("EXPLAIN output has no execution time")
    return float(match.group(1))


def set_state(engine: Engine, database_url: str, state: IndexState) -> None:
    """Put the indexes under test into `state`, then refresh planner statistics."""
    if state == PRODUCTION:
        with engine.begin() as conn:
            exclusion_toggle.ensure_test_database(conn)
            conn.execute(text(f"DROP INDEX IF EXISTS {BTREE_INDEX}"))
            exclusion_toggle.enable(conn)
        migrate(database_url, "head")
    else:
        with engine.begin() as conn:
            exclusion_toggle.disable(conn)
            if state == BTREE:
                conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {BTREE_INDEX} ON bookings (room_id)")
                )
            else:
                conn.execute(text(f"DROP INDEX IF EXISTS {BTREE_INDEX}"))
        migrate(database_url, REVISION_WITHOUT_QUERY_INDEXES)
    with engine.connect() as conn:
        conn.execute(text("ANALYZE bookings"))
        conn.commit()


def reseed(engine: Engine, database_url: str, today: date) -> dict[str, int]:
    set_state(engine, database_url, PRODUCTION)
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE payments, bookings, rooms, room_types, hotels, users "
                "RESTART IDENTITY CASCADE"
            )
        )
        counts = seed_dataset(conn, SeedConfig(), today)
    with engine.connect() as conn:
        conn.execute(text("ANALYZE"))
        conn.commit()
    return counts


def index_sizes(conn: Connection, names: Sequence[str]) -> dict[str, str]:
    query = text("SELECT pg_size_pretty(pg_relation_size(CAST(:name AS regclass)))")
    return {name: str(conn.scalar(query, {"name": name})) for name in names}


def measure(
    engine: Engine, database_url: str, workloads: Sequence[Workload], warmup: int
) -> tuple[list[Measurement], dict[str, str]]:
    explain_dir = RESULTS_DIR / "explain"
    explain_dir.mkdir(parents=True, exist_ok=True)
    measurements = []
    sizes: dict[str, str] = {}
    for state in STATES:
        set_state(engine, database_url, state)
        with engine.connect() as conn:
            if state == BTREE:
                sizes |= index_sizes(conn, [BTREE_INDEX])
            if state == PRODUCTION:
                sizes |= index_sizes(conn, [exclusion_toggle.CONSTRAINT_NAME, HELD_EXPIRY_INDEX])
            for workload in workloads:
                plan = explain(conn, workload.statement, workload.params[0])
                (explain_dir / f"{workload.name}_{state.name}.txt").write_text(
                    plan + "\n", encoding="utf-8"
                )
                timings = time_workload(conn, workload, warmup)
                measurement = Measurement(state, workload.name, timings, _execution_ms(plan))
                measurements.append(measurement)
                logger.info(
                    "workload measured",
                    extra={
                        "state": state.name,
                        "workload": workload.name,
                        "p50_ms": round(measurement.p50, 3),
                        "p95_ms": round(measurement.p95, 3),
                    },
                )
    return measurements, sizes


def write_report(
    measurements: list[Measurement],
    workloads: Sequence[Workload],
    counts: dict[str, int],
    sizes: dict[str, str],
    env: dict[str, str],
    warmup: int,
) -> None:
    write_csv(
        RESULTS_DIR / "search_benchmark.csv",
        ["state", "query", "executions", "p50_ms", "p95_ms", "max_ms", "explain_execution_ms"],
        [
            [
                m.state.name, m.workload, len(m.timings_ms), f"{m.p50:.3f}", f"{m.p95:.3f}",
                f"{max(m.timings_ms):.3f}", f"{m.explain_ms:.3f}",
            ]
            for m in measurements
        ],
    )  # fmt: skip

    lookup = {(m.state, m.workload): m for m in measurements}
    latency_headers = ["Query"]
    for state in STATES:
        latency_headers += [f"{state.name} p50 (ms)", f"{state.name} p95 (ms)"]
    latency_rows = []
    for w in workloads:
        row: list[object] = [f"`{w.name}`"]
        for state in STATES:
            row += [f"{lookup[(state, w.name)].p50:.2f}", f"{lookup[(state, w.name)].p95:.2f}"]
        latency_rows.append(row)

    plan_rows = [
        [f"`{w.name}`"]
        + [f"{lookup[(state, w.name)].explain_ms:.3f}" for state in STATES]
        + [", ".join(f"[{s.name}](explain/{w.name}_{s.name}.txt)" for s in STATES)]
        for w in workloads
    ]
    plans = markdown_table(["Query"] + [f"{s.name} (ms)" for s in STATES] + ["Plans"], plan_rows)
    states = "\n".join(f"- **{s.name}**: {s.description}" for s in STATES)
    queries = "\n".join(f"- `{w.name}`: {w.description}" for w in workloads)
    dataset = ", ".join(f"{v:,} {k}" for k, v in counts.items())
    size_lines = "\n".join(f"- `{name}`: {size}" for name, size in sizes.items())
    env_lines = "\n".join(f"- {k}: {v}" for k, v in env.items())
    executions = len(workloads[0].params)

    report = f"""# Search benchmark

Generated by `python -m scripts.benchmark_search`. Every number below comes from that run;
raw rows are in `search_benchmark.csv`.

## Setup

- Dataset: {dataset} (`scripts.seed` defaults, fixed random seed).
- Queries, exactly as the application sends them (same SQL, same parameter types):
{queries}
- Index states:
{states}
- Dropping the exclusion constraint also removes the double-booking guarantee, which is why
  this only ever happens on the test database.
- Each query and state: {warmup} warm-up executions, then {executions} timed executions over a
  fixed, seeded list of cities, hotels, dates and party sizes. Every state uses the same list.
- Latency is client-side wall time (execute + fetch), so it includes the round trip from
  Python to PostgreSQL in Docker on the same machine. EXPLAIN times are server-side only,
  for the first parameter set.
{env_lines}

## Latency

{markdown_table(latency_headers, latency_rows)}

## EXPLAIN ANALYZE execution time (first parameter set)

{plans}

## Index sizes

{size_lines}
"""
    (RESULTS_DIR / "search_benchmark.md").write_text(report, encoding="utf-8")


def main() -> int:
    """Entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(description="Benchmark search queries with/without indexes.")
    parser.add_argument("--iterations", type=int, default=100, help="Timed executions per query.")
    parser.add_argument("--warmup", type=int, default=10, help="Untimed executions first.")
    args = parser.parse_args()

    configure_logging(get_settings().log_level)
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
        today = date.today()
        started = time.perf_counter()
        counts = reseed(engine, database_url, today)
        logger.info(
            "test database seeded",
            extra={**counts, "seconds": round(time.perf_counter() - started, 1)},
        )
        with engine.connect() as conn:
            workloads = build_workloads(conn, args.iterations, today)
        measurements, sizes = measure(engine, database_url, workloads, args.warmup)
        write_report(measurements, workloads, counts, sizes, env, args.warmup)
    except ExperimentSetupError as exc:
        logger.error(str(exc))
        return 1
    finally:
        set_state(engine, database_url, PRODUCTION)
        engine.dispose()

    logger.info("results written", extra={"directory": str(RESULTS_DIR)})
    return 0


if __name__ == "__main__":
    sys.exit(main())

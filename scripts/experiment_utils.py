"""Helpers shared by the experiment scripts: test-database access, statistics, reporting."""

import csv
import math
import os
import platform
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "results"


class ExperimentSetupError(RuntimeError):
    """Raised when an experiment can't run in the current configuration."""


def experiment_database_url() -> str:
    """The only database the experiments are allowed to modify."""
    url = get_settings().test_database_url
    if not url:
        raise ExperimentSetupError("TEST_DATABASE_URL is not set; see .env.example")
    return url


def migrate(database_url: str, revision: str) -> None:
    """Move the schema to `revision` ("head", or e.g. "0002" to go down)."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    if revision == "head":
        command.upgrade(cfg, "head")
    else:
        command.downgrade(cfg, revision)


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile: the smallest value with at least pct% of values at or below."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _git_revision() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit} (uncommitted changes)" if dirty else commit


def environment(conn: Connection) -> dict[str, str]:
    """Where and when a run happened, recorded next to its numbers."""
    return {
        "Run at (UTC)": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "Git revision": _git_revision(),
        "OS": platform.platform(),
        "CPU": f"{platform.processor() or 'unknown'} ({os.cpu_count()} logical cores)",
        "Python": sys.version.split()[0],
        "PostgreSQL": str(conn.scalar(text("SHOW server_version"))),
    }


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render rows as a GitHub-flavoured Markdown table."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def write_csv(path: Path, headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    """Write rows to a CSV file with a header line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)

import pytest

from scripts.concurrency_test import SCENARIOS, Outcome, RunResult
from scripts.experiment_utils import markdown_table, percentile


@pytest.mark.parametrize(
    ("pct", "expected"),
    [(50, 5), (95, 10), (100, 10), (10, 1), (1, 1)],
)
def test_percentile_nearest_rank(pct: float, expected: float) -> None:
    assert percentile(list(range(10, 0, -1)), pct) == expected


def test_percentile_rejects_empty() -> None:
    with pytest.raises(ValueError):
        percentile([], 50)


def test_markdown_table() -> None:
    assert markdown_table(["a", "b"], [[1, 2]]) == "| a | b |\n|---|---|\n| 1 | 2 |"


def test_run_result_classifies_outcomes() -> None:
    result = RunResult(
        scenario=SCENARIOS[0],
        run=1,
        outcomes=[
            Outcome(201, None, 0.1),
            Outcome(409, "room_unavailable", 0.1),
            Outcome(409, "room_unavailable", 0.1),
            Outcome(500, "internal_error", 0.1),
            Outcome(None, "ReadTimeout", 0.1),
        ],
        total_seconds=0.2,
        active_bookings=1,
        overlapping_pairs=0,
    )
    assert (result.booked, result.conflicts) == (1, 2)
    assert result.other_errors == {"500 internal_error": 1, "no response ReadTimeout": 1}

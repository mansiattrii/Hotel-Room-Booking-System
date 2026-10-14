import json
import logging
from collections.abc import Iterator

import pytest

from app.logging_config import configure_logging

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def restore_logging() -> Iterator[None]:
    loggers = [logging.getLogger(), *(logging.getLogger(name) for name in UVICORN_LOGGERS)]
    saved = [(lg, lg.handlers[:], lg.level, lg.propagate) for lg in loggers]
    yield
    for lg, handlers, level, propagate in saved:
        lg.handlers = handlers
        lg.setLevel(level)
        lg.propagate = propagate


def test_uvicorn_logs_are_json_lines(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # What uvicorn does before loading the app: its own plain-text handler, no propagation.
    access = logging.getLogger("uvicorn.access")
    access.handlers = [logging.StreamHandler()]
    access.propagate = False

    configure_logging("INFO")
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.0.1:0",
        "GET",
        "/api/v1/health",
        "1.1",
        200,
        extra={"color_message": "\x1b[1mGET\x1b[0m"},
    )

    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["logger"] == "uvicorn.access"
    assert record["msg"] == '10.0.0.1:0 - "GET /api/v1/health HTTP/1.1" 200'
    assert "color_message" not in record

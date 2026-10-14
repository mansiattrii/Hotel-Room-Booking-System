import json
import logging
from datetime import UTC, datetime
from typing import Any

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    """Render each log record as one JSON object per line.

    Anything passed through ``extra=`` is included as top-level fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    """Send all application logs to stderr as JSON lines."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

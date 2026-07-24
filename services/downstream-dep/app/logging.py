"""Structured JSON logging to stdout.

Every log line is a single JSON object with the fields mandated by the project
conventions: timestamp, service, level, message, trace_id. Any extra key/values
passed via ``logger.info(msg, extra={...})`` are merged into the object.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# Set per-request by the HTTP middleware; read as a fallback by JsonFormatter so
# every log line emitted while handling a request carries its trace_id.
current_trace_id: ContextVar[str | None] = ContextVar("current_trace_id", default=None)

# Attributes present on every LogRecord that we don't want to echo as "extra".
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class _StdoutHandler(logging.StreamHandler):
    """StreamHandler that always writes to the *current* sys.stdout.

    Binding sys.stdout once at construction breaks under test capture (and any
    stdout redirection), so we resolve it dynamically on every emit.
    """

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value):  # ignore the base class's cached assignment
        pass


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z",
            "service": self.service,
            "level": record.levelname,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None) or current_trace_id.get(),
        }
        # Merge any user-supplied extras (e.g. delay_ms, config_version).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        return json.dumps(payload)


def get_logger(service: str) -> logging.Logger:
    """Return a stdout JSON logger for ``service`` (idempotent)."""
    logger = logging.getLogger(service)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # avoid duplicate lines via the root logger
    if not logger.handlers:
        handler = _StdoutHandler()
        handler.setFormatter(JsonFormatter(service))
        logger.addHandler(handler)
    return logger

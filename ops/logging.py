"""Structured logging.

The service emits one JSON object per line. That is the whole point: a log line
is an *event*, and an event that can only be read by a human cannot be counted,
grouped or alerted on. "How many 5xx did the tasks endpoint return in the last
hour?" is a one-line query against JSON logs and an unbounded regex exercise
against free text.

Three pieces work together:

``request_id`` context
    A :class:`contextvars.ContextVar` holding the id of the request currently
    being handled. Because it is context-local, code deep in the call stack can
    read it without threading a parameter through every function signature.

:class:`RequestContextFilter`
    Runs on every record and guarantees ``request_id`` exists, so the formatter
    never has to defend against a missing field and downstream parsers never
    have to handle two different shapes.

:class:`JsonFormatter`
    Serialises the record to a stable schema, promotes anything passed through
    ``logger.info(..., extra={...})`` into real JSON fields, and expands
    exceptions into a structured object instead of one escaped blob.

A note on the previous implementation: an f-string formatter such as
``'{"msg":"%(message)s"}'`` produces invalid JSON the first time a log message
contains a quote or a newline, which is exactly when the log matters most. It
also cannot carry extra fields. Both problems disappear once the record is
serialised rather than interpolated.
"""
from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

REQUEST_ID_HEADER = "X-Request-ID"
MISSING = "-"

# Read from the environment rather than django.conf.settings: this module is
# imported *by* the settings module (the LOGGING dict references it), so touching
# settings here would be a circular import.
SERVICE_NAME = os.getenv("SERVICE_NAME", "cloud-ops-mini-stack")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.1.0")

_request_id: ContextVar[str] = ContextVar("request_id", default=MISSING)


def set_request_id(value: str | None) -> None:
    _request_id.set(value or MISSING)


def get_request_id() -> str:
    return _request_id.get()


def reset_request_id() -> None:
    _request_id.set(MISSING)


# Every attribute a freshly built LogRecord carries. Anything *not* in this set
# was supplied by the caller through `extra=` and therefore represents
# application context that belongs in the JSON payload.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of arbitrary values into JSON-safe ones."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


class RequestContextFilter(logging.Filter):
    """Attach the current request id to every record.

    A filter (rather than something the caller does) because correlation has to
    be impossible to forget. A log line emitted from a library, from a signal
    handler or from an exception handler deep in DRF still gets the id.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    """Serialise a :class:`logging.LogRecord` into a single-line JSON object.

    The schema is deliberately flat and fixed for the fields that always exist::

        {
          "ts": "2026-09-20T12:41:07.123Z",
          "level": "INFO",
          "logger": "app.access",
          "message": "http_request",
          "service": "cloud-ops-mini-stack",
          "version": "1.1.0",
          "request_id": "9f2c1a4b7e0d3856",
          "http": {"method": "GET", "endpoint": "/api/tasks/", "status": 200},
          "duration_ms": 12.4
        }

    ``message`` is the *event name*, not a sentence. Log messages that are
    sentences cannot be aggregated; event names can.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "request_id": getattr(record, "request_id", MISSING) or MISSING,
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _jsonable(value)

        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            payload["exception"] = {
                "type": getattr(exc_type, "__name__", None),
                "message": str(exc_value),
                "stacktrace": self.formatException(record.exc_info).splitlines(),
            }
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, default=_jsonable)


class HumanFormatter(logging.Formatter):
    """Readable output for a local terminal.

    Same records, different renderer. The event payload is appended as compact
    JSON so nothing is lost -- it is just moved out of the way.
    """

    _EXTRA_SKIP = _STANDARD_ATTRS | {"request_id"}

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: _jsonable(value)
            for key, value in record.__dict__.items()
            if key not in self._EXTRA_SKIP and not key.startswith("_")
        }
        request_id = getattr(record, "request_id", MISSING)
        suffix = f" [rid={request_id}]"
        if extras:
            suffix += " " + json.dumps(extras, ensure_ascii=False, default=_jsonable)
        line = f"{base}{suffix}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line

"""Tests for the structured logging layer.

The formatter is tested directly rather than through a log capture fixture,
because the contract being verified is the *schema* of the output: which keys
exist, what they are named, and that the result parses as JSON in the presence of
characters that break naive f-string formatters.
"""
from __future__ import annotations

import json
import logging

from ops.logging import (
    MISSING,
    HumanFormatter,
    JsonFormatter,
    RequestContextFilter,
    get_request_id,
    reset_request_id,
    set_request_id,
)


def make_record(message: str, level: int = logging.INFO, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=level,
        pathname=__file__,
        lineno=42,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestJsonFormatter:
    def test_output_is_single_line_json(self):
        record = make_record("http_request", request_id="abc123")
        formatted = JsonFormatter().format(record)

        assert "\n" not in formatted
        payload = json.loads(formatted)
        assert payload["message"] == "http_request"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "app.test"
        assert payload["request_id"] == "abc123"

    def test_every_record_carries_service_identity(self):
        payload = json.loads(JsonFormatter().format(make_record("boot")))
        assert payload["service"]
        assert payload["version"]

    def test_timestamp_is_iso8601_utc(self):
        payload = json.loads(JsonFormatter().format(make_record("boot")))
        assert payload["ts"].endswith("Z")
        assert "T" in payload["ts"]

    def test_extra_fields_are_promoted_to_top_level_json(self):
        record = make_record(
            "http_request",
            request_id="abc",
            duration_ms=12.5,
            http={"method": "GET", "status": 200, "endpoint": "/api/tasks/"},
        )
        payload = json.loads(JsonFormatter().format(record))

        assert payload["duration_ms"] == 12.5
        assert payload["http"] == {"method": "GET", "status": 200, "endpoint": "/api/tasks/"}

    def test_quotes_and_newlines_in_the_message_do_not_break_the_json(self):
        # The exact failure mode of an f-string formatter such as
        # '{"msg":"%(message)s"}': the output stops being parseable precisely
        # when the message contains something worth reading.
        nasty = 'client said "it\'s broken"\nsecond line\ttab \\ backslash'
        formatted = JsonFormatter().format(make_record(nasty))

        assert "\n" not in formatted
        assert json.loads(formatted)["message"] == nasty

    def test_exception_is_expanded_into_a_structured_object(self):
        try:
            raise ValueError("something went wrong")
        except ValueError:
            import sys

            record = make_record("boom", level=logging.ERROR)
            record.exc_info = sys.exc_info()

        payload = json.loads(JsonFormatter().format(record))
        assert payload["exception"]["type"] == "ValueError"
        assert payload["exception"]["message"] == "something went wrong"
        assert any("ValueError" in line for line in payload["exception"]["stacktrace"])

    def test_unserialisable_extra_values_are_coerced(self):
        from pathlib import Path
        from uuid import UUID

        record = make_record(
            "coerce",
            a_path=Path("C:/tmp/x.log"),
            a_uuid=UUID("12345678-1234-5678-1234-567812345678"),
            a_set={"b", "a"},
        )
        payload = json.loads(JsonFormatter().format(record))

        assert payload["a_path"].endswith("x.log")
        assert payload["a_uuid"] == "12345678-1234-5678-1234-567812345678"
        assert sorted(payload["a_set"]) == ["a", "b"]

    def test_non_ascii_is_preserved(self):
        payload = json.loads(JsonFormatter().format(make_record("数据库连接失败")))
        assert payload["message"] == "数据库连接失败"


class TestHumanFormatter:
    def test_renders_readable_output_with_the_request_id(self):
        record = make_record("http_request", request_id="abc123", duration_ms=9.0)
        output = HumanFormatter("%(levelname)s %(message)s").format(record)

        assert "INFO http_request" in output
        assert "[rid=abc123]" in output
        assert '"duration_ms": 9.0' in output


class TestRequestContext:
    def setup_method(self):
        reset_request_id()

    def teardown_method(self):
        reset_request_id()

    def test_context_variable_is_visible_from_anywhere_in_the_stack(self):
        set_request_id("from-middleware")
        assert get_request_id() == "from-middleware"

    def test_filter_fills_in_a_missing_request_id(self):
        set_request_id("ctx-1")
        record = make_record("orphan")
        assert RequestContextFilter().filter(record) is True
        assert record.request_id == "ctx-1"

    def test_filter_does_not_overwrite_an_explicit_request_id(self):
        set_request_id("ctx-1")
        record = make_record("explicit", request_id="explicit-1")
        RequestContextFilter().filter(record)
        assert record.request_id == "explicit-1"

    def test_outside_a_request_the_placeholder_is_used(self):
        record = make_record("management command output")
        RequestContextFilter().filter(record)
        assert record.request_id == MISSING

    def test_reset_clears_the_context(self):
        # A leaked context variable would make the next request's log lines
        # claim an id that belongs to a different request.
        set_request_id("ctx-1")
        reset_request_id()
        assert get_request_id() == MISSING

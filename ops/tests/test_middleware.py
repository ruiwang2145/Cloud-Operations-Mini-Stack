"""Tests for the request-scoped observability middleware.

Two contracts are verified here.

**Correlation.** Every request gets an id, the id is echoed on the response, and
a caller-supplied id is honoured rather than replaced -- that is what lets a
front-end error report and a back-end log line be joined on a single value.

**Metrics.** Every request produces RED samples, the endpoint label is the
normalised route, and an exception that reaches the middleware is still recorded
even though the response Django eventually sends never passes back through it.
"""
from __future__ import annotations

import logging

import pytest
from django.test import Client

from ops.tests.helpers import metric_value


@pytest.fixture
def access_logs(caplog):
    """Capture ``app.access`` records.

    The ``app`` logger sets ``propagate = False`` in the LOGGING config, so
    records never reach the root logger that pytest's ``caplog`` listens on. The
    capture handler is therefore attached directly to the logger under test.
    """
    logger = logging.getLogger("app.access")
    logger.addHandler(caplog.handler)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(previous_level)


@pytest.mark.django_db
class TestRequestId:
    def test_generates_an_id_when_the_caller_sends_none(self):
        response = Client().get("/healthz/")
        assert response["X-Request-ID"]
        assert len(response["X-Request-ID"]) == 16

    def test_honours_a_caller_supplied_id(self):
        # The id has to be the *same* value end to end, otherwise the front end
        # and the back end are quoting different numbers for one incident.
        response = Client().get("/healthz/", HTTP_X_REQUEST_ID="trace-from-the-edge")
        assert response["X-Request-ID"] == "trace-from-the-edge"

    def test_supplied_ids_are_truncated(self):
        response = Client().get("/healthz/", HTTP_X_REQUEST_ID="x" * 500)
        assert len(response["X-Request-ID"]) == 64

    def test_context_is_cleared_after_the_request(self):
        from ops.logging import MISSING, get_request_id

        Client().get("/healthz/", HTTP_X_REQUEST_ID="leaky")
        assert get_request_id() == MISSING

    def test_id_appears_in_the_access_log(self, access_logs):
        Client().get("/api/tasks/", HTTP_X_REQUEST_ID="correlate-me")
        records = [r for r in access_logs.records if r.message == "http_request"]
        assert records
        assert records[-1].request_id == "correlate-me"


@pytest.mark.django_db
class TestRedMetrics:
    def test_successful_request_is_counted_by_endpoint_and_status_class(self):
        before = metric_value(
            "app_http_requests_total",
            method="GET",
            endpoint="/api/tasks/",
            status_class="2xx",
        )
        Client().get("/api/tasks/")
        after = metric_value(
            "app_http_requests_total",
            method="GET",
            endpoint="/api/tasks/",
            status_class="2xx",
        )

        assert after == before + 1

    def test_client_errors_are_separated_from_server_errors(self):
        # A 403 from an unauthenticated write is not an availability failure.
        # Counting 4xx as errors would make the availability SLI useless.
        before = metric_value(
            "app_http_requests_total",
            method="POST",
            endpoint="/api/tasks/",
            status_class="4xx",
        )
        Client().post("/api/tasks/", data={"title": "x"}, content_type="application/json")
        after = metric_value(
            "app_http_requests_total",
            method="POST",
            endpoint="/api/tasks/",
            status_class="4xx",
        )

        assert after == before + 1

    def test_latency_is_observed(self):
        def observations() -> float:
            return metric_value(
                "app_http_request_duration_seconds_count",
                method="GET",
                endpoint="/api/tasks/",
            )

        before = observations()
        Client().get("/api/tasks/")
        assert observations() == before + 1

    def test_unhandled_exception_is_recorded_with_its_type(self, settings):
        # The 500 response Django sends for an exception is generated *above*
        # this middleware, so the only chance to record it is in the except
        # branch. Without that, the slowest and most interesting requests would
        # be the only ones missing from the metrics.
        settings.ENABLE_FAULT_ENDPOINTS = True
        before = metric_value(
            "app_errors_total", endpoint="/boom/", error_type="RuntimeError"
        )

        client = Client()
        client.raise_request_exception = False
        response = client.get("/boom/")

        assert response.status_code == 500
        after = metric_value("app_errors_total", endpoint="/boom/", error_type="RuntimeError")
        assert after == before + 1

    def test_access_log_records_method_endpoint_status_and_duration(self, access_logs):
        Client().get("/api/tasks/")
        record = [r for r in access_logs.records if r.message == "http_request"][-1]

        assert record.http["method"] == "GET"
        assert record.http["endpoint"] == "/api/tasks/"
        assert record.http["status"] == 200
        assert record.duration_ms >= 0

    def test_error_responses_are_logged_at_error_level(self, access_logs, settings):
        settings.ENABLE_FAULT_ENDPOINTS = True
        client = Client()
        client.raise_request_exception = False
        client.get("/boom/")

        record = [r for r in access_logs.records if r.message == "http_request"][-1]
        assert record.levelno == logging.ERROR
        assert record.http["status"] == 500

    def test_client_ip_prefers_the_forwarded_header(self, access_logs):
        Client().get("/api/tasks/", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1")
        record = [r for r in access_logs.records if r.message == "http_request"][-1]
        assert record.client_ip == "203.0.113.7"

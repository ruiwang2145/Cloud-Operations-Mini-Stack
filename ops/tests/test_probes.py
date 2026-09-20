"""Tests for the probe endpoints.

The behaviours worth pinning down here are the ones a future refactor would
quietly break: that liveness survives a database outage, that readiness fails
*closed* (503, not 200) when a dependency is down, and that the metrics and SLO
endpoints keep the promises the dashboards depend on.
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.core.cache import cache
from django.db import OperationalError, connections
from django.test import Client

from ops.slo import REPORT_CACHE_KEY, evaluate_slo, get_slo_report
from ops.tests.helpers import metric_value


def database_down():
    """Context manager that makes every database cursor raise."""
    return mock.patch.object(
        connections["default"],
        "cursor",
        side_effect=OperationalError("could not connect to server"),
    )


@pytest.mark.django_db
class TestLiveness:
    def test_returns_ok_even_when_the_database_is_down(self):
        # Liveness must survive a database outage. If it queried the database,
        # the orchestrator would restart every healthy worker in a loop during a
        # database incident -- turning someone else's outage into a self-inflicted
        # denial of service.
        with database_down():
            response = Client().get("/healthz/")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_reports_identity_and_uptime(self):
        payload = Client().get("/healthz/").json()
        assert payload["service"] == "cloud-ops-mini-stack"
        assert payload["version"]
        assert payload["uptime_seconds"] >= 0

    def test_is_never_cached(self):
        # A cached probe reports the state of the world as it was, which is the
        # opposite of what a probe is for.
        response = Client().get("/healthz/")
        assert "no-store" in response["Cache-Control"]


@pytest.mark.django_db
class TestReadiness:
    def test_ready_when_the_database_answers(self):
        response = Client().get("/readyz/")
        assert response.status_code == 200

        payload = response.json()
        assert payload["status"] == "ready"
        assert payload["checks"]["database"]["ok"] is True

    def test_not_ready_returns_503_when_the_database_is_down(self):
        with database_down():
            response = Client().get("/readyz/")

        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "not_ready"
        assert payload["checks"]["database"]["ok"] is False
        assert "OperationalError" in payload["checks"]["database"]["error"]

    def test_failure_is_counted_per_dependency(self):
        before = metric_value("app_readiness_failures_total", dependency="database")
        with database_down():
            Client().get("/readyz/")
        after = metric_value("app_readiness_failures_total", dependency="database")

        assert after == before + 1

    def test_reports_the_latency_of_the_dependency_check(self):
        payload = Client().get("/readyz/").json()
        assert payload["checks"]["database"]["latency_ms"] >= 0


@pytest.mark.django_db
class TestVersion:
    def test_reports_build_metadata(self):
        payload = Client().get("/version/").json()
        assert payload["service"] == "cloud-ops-mini-stack"
        assert "version" in payload
        assert payload["fault_endpoints_enabled"] is True


@pytest.mark.django_db
class TestMetricsEndpoint:
    def test_exposes_prometheus_text_format(self):
        response = Client().get("/metrics")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/plain")

        body = response.content.decode()
        assert "# TYPE app_http_requests_total counter" in body
        assert "# TYPE app_http_request_duration_seconds histogram" in body

    def test_exposes_the_framework_metrics_the_dashboards_rely_on(self):
        Client().get("/api/tasks/")
        body = Client().get("/metrics").content.decode()

        # These exact names are what monitoring/grafana/dashboards/*.json query.
        # Renaming one without updating the dashboards would silently empty a
        # panel, so the coupling is asserted rather than assumed. Note the
        # `_total_total` suffix: django-prometheus names the counter
        # `..._total` and prometheus_client appends another `_total`.
        for metric in (
            "django_http_responses_total_by_status_view_method_total",
            "django_http_requests_latency_seconds_by_view_method_bucket",
            "django_http_requests_total_by_view_transport_method_total",
        ):
            assert metric in body, metric

    def test_build_info_is_published(self):
        body = Client().get("/metrics").content.decode()
        assert "app_build_info{" in body
        assert 'environment="dev"' in body

    def test_metrics_is_not_served_at_the_trailing_slash_path(self):
        # Prometheus scrapes the literal path from its configuration, so the
        # missing slash is deliberate and worth a regression test.
        assert Client().get("/metrics/").status_code == 404


@pytest.mark.django_db
class TestSloEndpoint:
    def test_returns_a_report_and_always_200(self):
        # The endpoint reports a state; it is not itself a health check.
        # Something that has to tell "the SLO is breached" apart from "the SLO
        # endpoint is broken" needs those to be different status codes.
        Client().get("/api/tasks/")
        response = Client().get("/api/slo/")

        assert response.status_code == 200
        payload = response.json()
        assert payload["objectives"]["availability"] == 0.995
        assert payload["source"] in {"prometheus", "local_registry"}
        assert payload["status"] in {"healthy", "at_risk", "breached", "no_data"}

    def test_probe_traffic_is_excluded_from_the_sli(self):
        """Monitoring requests must not flatter the availability number."""
        client = Client()
        client.get("/api/tasks/")  # ensure there is some counted traffic
        before = evaluate_slo()["traffic"]["requests"]

        for _ in range(5):
            client.get("/healthz/")
            client.get("/readyz/")
            client.get("/version/")
            client.get("/metrics")

        after = evaluate_slo()["traffic"]["requests"]
        assert after == before

    def test_report_is_cached_between_calls(self):
        cache.delete(REPORT_CACHE_KEY)
        first = get_slo_report()
        cache.set(REPORT_CACHE_KEY, {**first, "cached_marker": True}, timeout=30)

        assert get_slo_report().get("cached_marker") is True
        cache.delete(REPORT_CACHE_KEY)

    def test_cache_can_be_bypassed(self):
        assert "cached_marker" not in get_slo_report(use_cache=False)

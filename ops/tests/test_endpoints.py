"""Tests for endpoint labelling -- the cardinality guarantee.

This is the one property of the metrics layer that can take a monitoring system
down, so it gets its own test file. The interesting assertion is not "the label
looks right", it is "a thousand distinct URLs produce one time series".
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from django.test import Client

from ops.endpoints import UNMATCHED, is_probe_endpoint, normalise_endpoint
from ops.tests.helpers import endpoint_labels


def fake_request(route: str | None, path: str = "/"):
    resolver_match = None if route is None else SimpleNamespace(route=route)
    return SimpleNamespace(resolver_match=resolver_match, path=path)


class TestRouteTidying:
    @pytest.mark.parametrize(
        ("route", "expected"),
        [
            # path() based routes
            ("healthz/", "/healthz/"),
            ("metrics", "/metrics"),
            ("api/slo/", "/api/slo/"),
            # DRF router routes are regexes
            ("api/tasks/$", "/api/tasks/"),
            ("api/tasks/(?P<pk>[^/.]+)/$", "/api/tasks/{pk}/"),
            ("api/tasks/stats/$", "/api/tasks/stats/"),
            # already-tidy input is left alone
            ("/already/tidy/", "/already/tidy/"),
        ],
    )
    def test_route_is_tidied_into_a_readable_label(self, route, expected):
        assert normalise_endpoint(fake_request(route)) == expected

    def test_unresolved_requests_collapse_to_a_single_label(self):
        # A scanner walking /admin.php, /.env, /wp-login.php ... must not be able
        # to create a time series per URL.
        assert normalise_endpoint(fake_request(None, "/admin.php")) == UNMATCHED
        assert normalise_endpoint(fake_request(None, "/.env")) == UNMATCHED
        assert normalise_endpoint(fake_request(None, "/anything/at/all")) == UNMATCHED

    def test_pathological_routes_are_truncated(self):
        label = normalise_endpoint(fake_request("x" * 500))
        assert len(label) == 120


@pytest.mark.django_db
class TestCardinalityInPractice:
    """Assertions on the *whole* label set, not on a diff.

    Prometheus counters are process-global and never reset, so a before/after
    diff depends on which tests ran first. Asserting that no label contains a raw
    identifier is both order-independent and a direct statement of the property
    that matters.
    """

    def test_many_ids_produce_one_series(self):
        """The headline guarantee: 60 distinct URLs, one metric label."""
        from tasks.models import Task

        client = Client()
        for index in range(60):
            task = Task.objects.create(title=f"task {index}")
            assert client.get(f"/api/tasks/{task.pk}/").status_code == 200

        labels = endpoint_labels()
        assert "/api/tasks/{pk}/" in labels

        # Not one series per id.
        assert not any(re.search(r"/\d+/", label) for label in labels), sorted(labels)

    def test_list_and_detail_are_distinct_series(self):
        from tasks.models import Task

        task = Task.objects.create(title="only one")
        client = Client()
        client.get("/api/tasks/")
        client.get(f"/api/tasks/{task.pk}/")

        labels = endpoint_labels()
        assert "/api/tasks/" in labels
        assert "/api/tasks/{pk}/" in labels

    def test_unmatched_requests_do_not_create_a_series_per_url(self):
        client = Client()
        for path in ("/nope-1", "/nope-2", "/nope-3"):
            assert client.get(path).status_code == 404

        labels = endpoint_labels()
        assert UNMATCHED in labels
        assert not any("nope" in label for label in labels)


class TestProbeExclusion:
    @pytest.mark.parametrize(
        "endpoint", ["/healthz/", "/readyz/", "/metrics", "/version/"]
    )
    def test_probe_endpoints_are_recognised(self, endpoint):
        assert is_probe_endpoint(endpoint)

    @pytest.mark.parametrize(
        "endpoint", ["/api/tasks/", "/api/slo/", "/boom/", UNMATCHED]
    )
    def test_business_endpoints_are_not_excluded(self, endpoint):
        assert not is_probe_endpoint(endpoint)

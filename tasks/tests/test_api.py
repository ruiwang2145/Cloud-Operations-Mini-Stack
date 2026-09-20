"""Tests for the task API.

Beyond the CRUD happy path, three properties are pinned down because they are
the ones that tend to regress silently:

* the error envelope is uniform across every failure mode;
* an unparseable filter is rejected rather than ignored;
* the number of queries a list request runs does not grow with the number of
  rows, so the day a relation is added the regression surfaces as a failing test
  instead of as a latency graph nobody is watching.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from tasks.models import Task


@pytest.fixture
def anon_client():
    return APIClient()


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="operator", password="irrelevant")


@pytest.fixture
def client(user):
    api_client = APIClient()
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def tasks(db):
    return [
        Task.objects.create(title="write the runbook", done=True, priority="high"),
        Task.objects.create(title="add the dashboard", done=False, priority="high"),
        Task.objects.create(title="tidy the backlog", done=False, priority="low"),
    ]


@pytest.mark.django_db
class TestReads:
    def test_list_is_publicly_readable(self, anon_client, tasks):
        response = anon_client.get("/api/tasks/")
        assert response.status_code == 200
        assert response.json()["count"] == 3

    def test_list_is_paginated(self, anon_client, tasks):
        payload = anon_client.get("/api/tasks/").json()
        for key in ("count", "next", "previous", "results"):
            assert key in payload

    def test_retrieve_a_single_task(self, anon_client, tasks):
        task = tasks[0]
        payload = anon_client.get(f"/api/tasks/{task.pk}/").json()
        assert payload["title"] == task.title
        assert payload["priority"] == "high"

    def test_unknown_id_is_a_404(self, anon_client, tasks):
        assert anon_client.get("/api/tasks/999999/").status_code == 404

    def test_api_root_lists_the_endpoints(self, anon_client):
        payload = anon_client.get("/api/").json()
        assert "tasks" in payload


@pytest.mark.django_db
class TestWrites:
    def test_anonymous_write_is_refused(self, anon_client):
        response = anon_client.post("/api/tasks/", {"title": "x"}, format="json")
        assert response.status_code == 403

    def test_authenticated_create_succeeds(self, client):
        response = client.post(
            "/api/tasks/",
            {"title": "ship the change", "priority": "high"},
            format="json",
        )
        assert response.status_code == 201
        assert Task.objects.filter(title="ship the change").exists()

    def test_created_timestamps_are_server_owned(self, client):
        response = client.post(
            "/api/tasks/",
            {"title": "no backdating", "created_at": "1999-01-01T00:00:00Z"},
            format="json",
        )
        assert response.status_code == 201
        assert response.json()["created_at"].startswith("20")

    def test_patch_updates_only_the_named_field(self, client, tasks):
        task = tasks[0]
        response = client.patch(
            f"/api/tasks/{task.pk}/", {"done": False}, format="json"
        )
        assert response.status_code == 200

        task.refresh_from_db()
        assert task.done is False
        assert task.title == "write the runbook"

    def test_delete_removes_the_row(self, client, tasks):
        task = tasks[0]
        assert client.delete(f"/api/tasks/{task.pk}/").status_code == 204
        assert not Task.objects.filter(pk=task.pk).exists()


@pytest.mark.django_db
class TestValidation:
    def test_blank_title_is_rejected_with_the_error_envelope(self, client):
        response = client.post("/api/tasks/", {"title": "   "}, format="json")

        assert response.status_code == 400
        body = response.json()
        assert set(body) == {"error"}
        assert body["error"]["type"] == "ValidationError"
        assert body["error"]["status"] == 400
        assert body["error"]["request_id"]

    def test_over_long_title_is_a_400_not_a_500(self, client):
        # Without serializer validation this reaches the database and comes back
        # as a driver error, i.e. a 500 for what is a client mistake.
        response = client.post("/api/tasks/", {"title": "x" * 200}, format="json")
        assert response.status_code == 400

    def test_titles_are_trimmed(self, client):
        response = client.post("/api/tasks/", {"title": "  padded  "}, format="json")
        assert response.json()["title"] == "padded"

    def test_invalid_priority_is_a_400(self, client):
        response = client.post(
            "/api/tasks/", {"title": "ok", "priority": "urgent"}, format="json"
        )
        assert response.status_code == 400

    def test_permission_error_uses_the_same_envelope(self, anon_client):
        response = anon_client.post("/api/tasks/", {"title": "x"}, format="json")
        body = response.json()

        assert set(body) == {"error"}
        assert body["error"]["type"] == "NotAuthenticated"
        assert body["error"]["request_id"]


@pytest.mark.django_db
class TestFilteringAndOrdering:
    def test_filter_by_done_true(self, anon_client, tasks):
        payload = anon_client.get("/api/tasks/?done=true").json()
        assert payload["count"] == 1
        assert payload["results"][0]["title"] == "write the runbook"

    def test_filter_by_done_false(self, anon_client, tasks):
        assert anon_client.get("/api/tasks/?done=false").json()["count"] == 2

    def test_boolean_aliases_are_accepted(self, anon_client, tasks):
        for value in ("1", "True", "yes", "on"):
            assert anon_client.get(f"/api/tasks/?done={value}").json()["count"] == 1

    def test_unparseable_boolean_is_rejected_not_ignored(self, anon_client, tasks):
        # Silently ignoring `?done=maybe` would return every row and look like it
        # worked, which is worse than an error.
        response = anon_client.get("/api/tasks/?done=maybe")
        assert response.status_code == 400
        assert response.json()["error"]["detail"]["done"]

    def test_filter_by_priority(self, anon_client, tasks):
        assert anon_client.get("/api/tasks/?priority=high").json()["count"] == 2

    def test_ordering_is_allow_listed(self, anon_client, tasks):
        titles = [
            row["title"]
            for row in anon_client.get("/api/tasks/?ordering=title").json()["results"]
        ]
        assert titles == sorted(titles)

    def test_unknown_ordering_field_is_ignored(self, anon_client, tasks):
        # OrderingFilter drops fields that are not in `ordering_fields`, so this
        # is not a vector for "order by an unindexed column".
        assert anon_client.get("/api/tasks/?ordering=notes").status_code == 200


@pytest.mark.django_db
class TestStats:
    def test_aggregates_in_one_response(self, anon_client, tasks):
        payload = anon_client.get("/api/tasks/stats/").json()

        assert payload["total"] == 3
        assert payload["completed"] == 1
        assert payload["open"] == 2
        assert payload["by_priority"] == {"high": 2, "low": 1}

    def test_stats_on_an_empty_table(self, anon_client):
        payload = anon_client.get("/api/tasks/stats/").json()
        assert payload["total"] == 0
        assert payload["by_priority"] == {}


@pytest.mark.django_db
class TestQueryCount:
    def test_list_query_count_does_not_grow_with_the_number_of_rows(self, anon_client):
        """The guard against N+1 queries.

        Asserting a constant offset would be brittle; asserting that the count is
        *independent of the row count* is the property that actually matters and
        it survives unrelated middleware changes.
        """
        for index in range(3):
            Task.objects.create(title=f"small {index}")
        with CaptureQueriesContext(connection) as few:
            anon_client.get("/api/tasks/")

        for index in range(40):
            Task.objects.create(title=f"many {index}")
        with CaptureQueriesContext(connection) as many:
            anon_client.get("/api/tasks/")

        assert len(few) == len(many)

    def test_stats_runs_a_bounded_number_of_queries(self, anon_client):
        for index in range(20):
            Task.objects.create(title=f"task {index}")
        with CaptureQueriesContext(connection) as queries:
            anon_client.get("/api/tasks/stats/")

        # Two aggregates, not one query per row.
        assert len(queries) <= 3

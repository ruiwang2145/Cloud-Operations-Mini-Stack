"""Task API views.

A ``ModelViewSet`` gives list/retrieve/create/update/partial-update/destroy from
one class. The value here is not the code it saves -- it is that all six
operations share one queryset, one permission class and one serialiser, so the
access rules cannot drift between them.

Two design decisions worth defending in a code review:

**Filtering is explicit.** ``?done=true`` and ``?priority=high`` are handled in
``get_queryset``. A ``django-filter`` backend would be shorter, but it also adds
a dependency and a second place where query semantics are defined; with two
filters, explicit wins.

**Query counts are asserted in tests.** ``select_related``/``prefetch_related``
are absent because there are no relations to load yet. The test that pins the
query count is there so that the day a relation *is* added, the regression shows
up as a failing test instead of as a latency graph nobody is looking at.
"""
from __future__ import annotations

from django.db.models import Count, Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from rest_framework.response import Response

from .models import Task
from .serializers import TaskSerializer

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


class TaskViewSet(viewsets.ModelViewSet):
    """CRUD for tasks.

    ``GET    /api/tasks/``            list (paginated, filterable, orderable)
    ``POST   /api/tasks/``            create (authentication required)
    ``GET    /api/tasks/{id}/``       retrieve
    ``PUT    /api/tasks/{id}/``       replace
    ``PATCH  /api/tasks/{id}/``       partial update
    ``DELETE /api/tasks/{id}/``       delete
    ``GET    /api/tasks/stats/``      aggregate counts
    """

    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    filter_backends = [OrderingFilter]
    # An allow-list, not "any model field": ordering by an unindexed column on a
    # large table is a denial-of-service vector that any anonymous caller can
    # trigger with a query string.
    ordering_fields = ["created_at", "updated_at", "title", "priority"]
    ordering = ["-created_at"]

    def get_queryset(self):
        queryset = Task.objects.all()

        done = self.request.query_params.get("done")
        if done is not None:
            parsed = _parse_bool(done)
            if parsed is None:
                # An unparseable filter value is a client error, not a silently
                # ignored parameter. Ignoring it would return every row and look
                # like it worked.
                raise ValidationError({"done": "Expected a boolean (true/false)."})
            queryset = queryset.filter(done=parsed)

        priority = self.request.query_params.get("priority")
        if priority:
            queryset = queryset.filter(priority=priority)

        return queryset

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        """Aggregate counts in one query.

        Aggregating in the database rather than in Python keeps the response time
        flat as the table grows -- which is also why this is a separate endpoint
        instead of a field on the list response.

        Note the aliases: naming one of them ``done`` would collide with the
        model field of the same name, and Django would resolve ``Q(done=True)``
        against the aggregate instead of the column. That raises
        ``Cannot compute Count('done'): 'done' is an aggregate``, which is a
        confusing error for what is really a naming mistake.
        """
        aggregates = Task.objects.aggregate(
            total=Count("id"),
            completed=Count("id", filter=Q(done=True)),
            open=Count("id", filter=Q(done=False)),
        )
        by_priority = {
            row["priority"]: row["count"]
            for row in Task.objects.values("priority").annotate(count=Count("id"))
        }
        return Response({**aggregates, "by_priority": by_priority})


def _parse_bool(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return None

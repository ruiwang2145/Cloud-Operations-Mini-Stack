"""Domain model.

There is exactly one model, and that is a deliberate choice. The project exists
to demonstrate *operating* a service -- probes, metrics, SLOs, runbooks -- not to
model a business domain. A single, realistic model with a foreign-key-free shape
keeps the interesting part of the repository in ``ops/`` instead of hiding it
behind a schema nobody will read.

The fields are the ones that make the API interesting to monitor: an enum to
filter on, a mutable boolean so writes produce real latency in the histogram, and
timestamps so ordering is meaningful.
"""
from django.db import models


class Task(models.Model):
    """A unit of work tracked by the demo API."""

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    title = models.CharField(max_length=100)
    notes = models.TextField(blank=True, default="")
    done = models.BooleanField(default=False)
    priority = models.CharField(
        max_length=8,
        choices=Priority.choices,
        default=Priority.MEDIUM,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Default ordering lives on the model, not in the view. Every list --
        # the API, the admin, a shell query -- then agrees on what "first" means,
        # and pagination is stable rather than depending on database row order.
        ordering = ["-created_at"]
        # The API's only filter is `done`, and the list view always sorts by
        # created_at. One composite index serves both.
        indexes = [models.Index(fields=["done", "-created_at"])]

    def __str__(self) -> str:
        return self.title

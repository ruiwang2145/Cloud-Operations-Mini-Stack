"""Serialisation for the task API.

The serializer is the contract boundary, so validation lives here rather than in
the view or the model. The distinction matters: a ``CharField(max_length=100)``
on the model is enforced by the *database*, which means an over-long title raises
a 500 from the driver instead of a 400 to the caller. The serializer turns the
same constraint into a field-level error message.
"""
from __future__ import annotations

from rest_framework import serializers

from .models import Task


class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = Task
        fields = [
            "id",
            "title",
            "notes",
            "done",
            "priority",
            "created_at",
            "updated_at",
        ]
        # Server-owned fields are read-only so a client cannot backdate a record
        # by posting a `created_at`.
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_title(self, value: str) -> str:
        """Normalise before validating.

        Trimming first means ``"   "`` is rejected as blank rather than accepted
        as three characters, and ``" Buy milk "`` and ``"Buy milk"`` are the same
        task instead of two.
        """
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Title must not be blank.")
        return cleaned

    def validate_notes(self, value: str) -> str:
        return value.strip()

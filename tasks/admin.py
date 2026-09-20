"""Admin registration.

The admin is not the product, but during an incident it is the fastest way to
look at the data. A few lines here turn "let me open a database shell" into a
filterable list.
"""
from django.contrib import admin

from .models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "done", "priority", "created_at", "updated_at")
    list_filter = ("done", "priority")
    search_fields = ("title", "notes")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

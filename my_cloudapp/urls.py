"""Root URL configuration.

Read this together with ``ops/urls.py`` and ``tasks/urls.py``.

Ordering note: ``ops.urls`` is mounted at the empty prefix because it owns the
operational endpoints that must live at the root (/healthz/, /readyz/, /metrics).
Django keeps trying the remaining top-level patterns when a nested resolver
raises ``Resolver404``, so a request for ``/api/tasks/`` falls through to the
``api/`` include below instead of 404-ing. The order is deliberate, not
accidental, and ``ops/tests/test_routing.py`` pins it down.
"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("ops.urls")),
    path("api/", include("tasks.urls")),
]

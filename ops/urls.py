"""URL routes for the operational surface.

Mounted at the empty prefix by ``my_cloudapp.urls`` so that probes live at the
root, where every monitoring tool expects to find them and where they are
reachable even if the API is behind a different path or a different vhost.
"""
from django.urls import path
from django_prometheus.exports import ExportToDjangoView

from . import views

urlpatterns = [
    # Probes and metadata
    path("healthz/", views.liveness, name="liveness"),
    path("readyz/", views.readiness, name="readiness"),
    path("version/", views.version, name="version"),
    # The Prometheus scrape target.
    #
    # Registered explicitly rather than through `include("django_prometheus.urls")`
    # so that the path is visible in this file. Note the deliberate absence of a
    # trailing slash: Prometheus scrapes the literal path from its config, and
    # Django's APPEND_SLASH does not strip slashes, so `/metrics/` would 404 while
    # `/metrics` works. Getting this wrong produces a scrape config that looks
    # correct and silently collects nothing.
    path("metrics", ExportToDjangoView, name="prometheus-metrics"),
    # SLO reporting
    path("api/slo/", views.slo_status, name="slo-status"),
    # Fault injection, gated by ENABLE_FAULT_ENDPOINTS
    path("boom/", views.boom, name="fault-boom"),
    path("sentry-debug/", views.sentry_debug, name="fault-sentry"),
]

"""Prometheus metrics owned by the application.

Two families of series live here.

**Framework metrics** come from ``django-prometheus`` and are already exported on
``/metrics``: request counts by view/method/transport, response counts by status,
and request latency histograms. They answer "what is the framework doing?".

**Service metrics** are defined below. They exist because a dashboard should not
have to know how Django names its internals, and because the SLI queries need
labels that the framework does not provide:

``app_http_requests_total``
    The denominator of the availability SLI. Carries a *normalised* endpoint
    label, so a thousand task ids are one time series rather than a thousand.
``app_http_request_duration_seconds``
    The latency SLI. The bucket boundaries are chosen deliberately: 0.3 s is an
    actual bucket, so "fraction of requests faster than the 300 ms objective" is
    an exact lookup (``le="0.3"``) instead of a histogram_quantile
    interpolation that is only approximately right at the boundary.
``app_errors_total``
    Server-side errors, labelled by exception type. Separates "the client sent
    nonsense" (4xx, not an error) from "we broke" (5xx and unhandled exceptions).
``app_slo_*``
    Gauges written by the SLO reporter, so the objective and the measurement can
    be graphed on the same axis.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

# Histogram bucket boundaries, in seconds. 0.3 is present on purpose: the latency
# objective is 300 ms, and a bucket that lands exactly on the objective makes
# "what fraction of requests met the objective?" an exact lookup rather than a
# histogram_quantile interpolation. The boundaries below 300 ms are there to make
# the latency *distribution* legible in Grafana.
LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0)

# --------------------------------------------------------------------------- #
# RED metrics (Rate, Errors, Duration) for every request
# --------------------------------------------------------------------------- #
REQUESTS = Counter(
    "app_http_requests_total",
    "HTTP requests handled, by normalised endpoint and response status class.",
    ["method", "endpoint", "status_class"],
)

REQUEST_LATENCY = Histogram(
    "app_http_request_duration_seconds",
    "Server-side request duration in seconds, by normalised endpoint.",
    ["method", "endpoint"],
    buckets=LATENCY_BUCKETS,
)

ERRORS = Counter(
    "app_errors_total",
    "Server-side errors, by normalised endpoint and exception type.",
    ["endpoint", "error_type"],
)

BUILD_INFO = Info("app_build", "Build and runtime metadata for the running service.")


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
READINESS_FAILURES = Counter(
    "app_readiness_failures_total",
    "Readiness probe failures, by dependency.",
    ["dependency"],
)


# --------------------------------------------------------------------------- #
# SLO state
# --------------------------------------------------------------------------- #
SLO_AVAILABILITY = Gauge(
    "app_slo_availability_ratio",
    "Measured availability ratio over the SLO window.",
)
SLO_ERROR_BUDGET = Gauge(
    "app_slo_error_budget_remaining_ratio",
    "Fraction of the availability error budget still unspent (1.0 = nothing spent).",
)
SLO_BURN_RATE = Gauge(
    "app_slo_burn_rate",
    "Speed of error-budget consumption (1.0 = exactly on target, >1 = over budget).",
)
SLO_LATENCY_COMPLIANCE = Gauge(
    "app_slo_latency_compliance_ratio",
    "Share of requests that met the latency objective.",
)
SLO_TARGET_MET = Gauge(
    "app_slo_target_met",
    "1 when every objective is currently met, 0 otherwise.",
)
SLO_LAST_EVALUATION = Gauge(
    "app_slo_last_evaluation_timestamp_seconds",
    "Unix timestamp of the last successful SLO evaluation.",
)
SLO_EVALUATION_FALLBACKS = Counter(
    "app_slo_evaluation_fallbacks_total",
    "SLO evaluations where a configured Prometheus could not be queried.",
    ["reason"],
)


def status_class(status_code: int) -> str:
    """Collapse an HTTP status into its class: ``2xx``, ``4xx``, ``5xx``.

    Labelling by the exact status code multiplies the series count by the number
    of distinct codes in use, and no SLO is written against "502 specifically".
    """
    return f"{status_code // 100}xx"


def describe_build(service: str, version: str, environment: str) -> None:
    """Publish build metadata as an ``app_build_info`` series.

    An Info metric is a constant 1 with labels. It costs nothing and makes
    "which version is actually serving traffic?" a query instead of a guess.
    """
    BUILD_INFO.info({"service": service, "version": version, "environment": environment})

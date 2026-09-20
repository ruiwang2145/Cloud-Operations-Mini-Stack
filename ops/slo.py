"""SLO evaluation: from raw counters to "are we still inside our error budget?"

The service publishes raw counters on ``/metrics``. Prometheus scrapes them and
retains history. This module answers the question an operator actually has,
which is not "what is the request rate?" but:

    Are we currently meeting our objectives, and at this rate, how long until we
    are not?

Three concepts do the work.

**SLI (indicator)** -- a measured ratio. Availability is
``1 - (5xx / total)``; latency compliance is
``requests_faster_than_300ms / total``. Both are dimensionless ratios over the
same window, which is what makes them comparable across endpoints and services.

**SLO (objective)** -- the target for an SLI, e.g. 99.5% availability. A target
below 100% is not laziness: it is an explicit statement that some failures are
acceptable, which is what lets you decide whether to ship a feature or chase a
flaky test.

**Error budget** -- the failures the objective permits: ``1 - 0.995`` = 0.5% of
requests. The budget converts an abstract percentage into a currency that
product and engineering can argue about. **Burn rate** is how fast it is being
spent: 1.0 means "on target, the budget lasts exactly the window"; 4.0 means
"the budget is gone in a quarter of the window".

Two data sources, in order of preference:

1. ``prometheus`` -- the production path. Only Prometheus holds enough history to
   honour a 30-day window, and it is the only source that keeps working when the
   application itself is down (which is precisely when you need it).
2. ``local`` -- the in-process registry, used when ``PROMETHEUS_URL`` is unset or
   unreachable. The window is then "since process start". The response says so
   explicitly in ``window_source`` rather than presenting a five-minute-old
   process as if it had a month of history.

The arithmetic lives in :func:`compute_slo`, which is a pure function of numbers
and has no I/O, so it is unit tested directly (see ``ops/tests/test_slo.py``).
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from django.conf import settings
from django.core.cache import cache
from prometheus_client import REGISTRY

from .endpoints import PROBE_ENDPOINTS, is_probe_endpoint
from .metrics import (
    LATENCY_BUCKETS,
    SLO_AVAILABILITY,
    SLO_BURN_RATE,
    SLO_ERROR_BUDGET,
    SLO_EVALUATION_FALLBACKS,
    SLO_LAST_EVALUATION,
    SLO_LATENCY_COMPLIANCE,
    SLO_TARGET_MET,
)

logger = logging.getLogger("app.slo")

REPORT_CACHE_KEY = "ops:slo:report"

# PromQL label matcher that keeps monitoring traffic out of every SLI.
_PROBE_MATCHER = 'endpoint!~"%s"' % "|".join(sorted(PROBE_ENDPOINTS))


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SLO:
    """The service level objectives, read from Django settings.

    Kept as a value object so it can be passed around, logged and asserted on
    without touching global state. ``scripts/render_rules.py`` renders the
    Prometheus alert rules from the same source, which is how the dashboards, the
    alerts and this endpoint stay in agreement.
    """

    availability_target: float
    latency_target_ms: int
    latency_compliance_target: float
    window: str

    @classmethod
    def from_settings(cls) -> "SLO":
        return cls(
            availability_target=float(settings.SLO_AVAILABILITY_TARGET),
            latency_target_ms=int(settings.SLO_LATENCY_TARGET_MS),
            latency_compliance_target=float(settings.SLO_LATENCY_COMPLIANCE_TARGET),
            window=str(settings.SLO_WINDOW),
        )

    @property
    def latency_bucket_seconds(self) -> float:
        """Smallest histogram bucket that is >= the latency objective.

        If the objective is not a bucket boundary, the nearest bucket above it is
        used and reported back, so nobody reads a number computed against a
        threshold they did not choose.
        """
        target = self.latency_target_ms / 1000.0
        for bound in sorted(LATENCY_BUCKETS):
            if bound >= target:
                return bound
        return float(max(LATENCY_BUCKETS))

    @property
    def error_budget_ratio(self) -> float:
        """Fraction of requests the availability objective allows to fail."""
        return max(1e-9, 1.0 - self.availability_target)


# --------------------------------------------------------------------------- #
# Prometheus as a data source
# --------------------------------------------------------------------------- #
class PrometheusUnavailable(RuntimeError):
    """Raised when a configured Prometheus cannot answer a query."""


class PrometheusClient:
    """Minimal Prometheus HTTP API client.

    Deliberately stdlib-only (``urllib``). The project needs exactly one
    endpoint of one API, and adding a dependency to call it would be more moving
    parts than the call itself.
    """

    def __init__(self, base_url: str, timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def query(self, expression: str) -> float | None:
        """Run an instant query and return the sum of the returned series.

        ``None`` means "Prometheus answered, but has no data" -- which is not an
        error. It is the correct answer for a service that started five minutes
        ago when the question is about the last thirty days.
        """
        url = f"{self.base_url}/api/v1/query?{urlencode({'query': expression})}"
        try:
            with urlopen(url, timeout=self.timeout) as response:  # noqa: S310 - operator-supplied URL
                payload = json.load(response)
        except (URLError, OSError, TimeoutError) as exc:
            raise PrometheusUnavailable(str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise PrometheusUnavailable(f"invalid JSON from Prometheus: {exc}") from exc

        if payload.get("status") != "success":
            raise PrometheusUnavailable(str(payload.get("error") or "query failed"))

        result = payload.get("data", {}).get("result") or []
        if not result:
            return None
        return sum(float(series["value"][1]) for series in result)

    def snapshot(self, slo: SLO) -> dict[str, float | None]:
        window = slo.window
        bucket = _format_bound(slo.latency_bucket_seconds)

        return {
            "requests_total": self.query(
                f'sum(rate(app_http_requests_total{{{_PROBE_MATCHER}}}[{window}]))'
            ),
            "errors_total": self.query(
                f'sum(rate(app_http_requests_total{{status_class="5xx",{_PROBE_MATCHER}}}[{window}]))'
            ),
            "latency_total": self.query(
                f'sum(rate(app_http_request_duration_seconds_count{{{_PROBE_MATCHER}}}[{window}]))'
            ),
            "latency_good": self.query(
                "sum(rate(app_http_request_duration_seconds_bucket"
                f'{{le="{bucket}",{_PROBE_MATCHER}}}[{window}]))'
            ),
        }


def _format_bound(value: float) -> str:
    """Format a bucket bound the way Prometheus stores the ``le`` label."""
    return f"{value:g}"


# --------------------------------------------------------------------------- #
# The in-process registry as a data source
# --------------------------------------------------------------------------- #
def _local_snapshot(latency_bucket: float) -> dict[str, float | None]:
    """Read the SLI inputs straight out of the process's own registry.

    The window is the lifetime of the process. That is a real limitation and the
    response reports it, rather than pretending a fresh process has a month of
    history behind it.
    """
    requests_total = 0.0
    errors_total = 0.0
    latency_total = 0.0
    latency_good = 0.0

    for family in REGISTRY.collect():
        for sample in family.samples:
            endpoint = sample.labels.get("endpoint", "")
            if is_probe_endpoint(endpoint):
                continue

            if sample.name == "app_http_requests_total":
                requests_total += sample.value
                if sample.labels.get("status_class") == "5xx":
                    errors_total += sample.value
            elif sample.name == "app_http_request_duration_seconds_count":
                latency_total += sample.value
            elif sample.name == "app_http_request_duration_seconds_bucket":
                raw_bound = sample.labels.get("le")
                if raw_bound is None:
                    continue
                try:
                    bound = float(raw_bound)
                except ValueError:
                    bound = math.inf  # the +Inf bucket
                if bound <= latency_bucket:
                    latency_good += sample.value

    return {
        "requests_total": requests_total,
        "errors_total": errors_total,
        "latency_total": latency_total,
        "latency_good": latency_good,
    }


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #
def compute_slo(
    *,
    snapshot: dict[str, float | None],
    slo: SLO,
    source: str,
    window_source: str,
    service: str = "cloud-ops-mini-stack",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Turn SLI inputs into an SLO report.

    Pure: no clock, no network, no globals. Everything that makes this hard to
    test is injected, which is why ``ops/tests/test_slo.py`` can cover the
    arithmetic -- including the divisions by zero -- without mocking anything.
    """
    generated_at = generated_at or datetime.now(timezone.utc)
    requests_total = float(snapshot.get("requests_total") or 0.0)
    errors_total = float(snapshot.get("errors_total") or 0.0)
    latency_total = float(snapshot.get("latency_total") or 0.0)
    latency_good = float(snapshot.get("latency_good") or 0.0)

    report: dict[str, Any] = {
        "service": service,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "window": slo.window,
        "window_source": window_source,
        "source": source,
        "objectives": {
            "availability": slo.availability_target,
            "latency_threshold_ms": slo.latency_target_ms,
            "latency_compliance": slo.latency_compliance_target,
        },
        "traffic": {
            "requests": round(requests_total, 4),
            "errors": round(errors_total, 4),
            "latency_samples": round(latency_total, 4),
        },
    }

    if requests_total <= 0:
        # A brand-new deployment, or a window with no traffic. Returning 100%
        # availability here would be a lie that hides a broken scrape config.
        report["status"] = "no_data"
        report["availability"] = {
            "sli": None,
            "objective": slo.availability_target,
            "met": None,
            "error_budget_remaining": None,
            "burn_rate": None,
            "errors": 0.0,
        }
        report["latency"] = {
            "sli": None,
            "objective": slo.latency_compliance_target,
            "met": None,
            "threshold_ms": slo.latency_target_ms,
            "bucket_ms": round(slo.latency_bucket_seconds * 1000, 3),
        }
        report["summary"] = "No traffic recorded in the evaluation window."
        return report

    # Round first, then decide.
    #
    # Floating point makes `1 - 0.0025` slightly less than 0.9975, so a burn
    # rate that is mathematically exactly 0.5 computes as 0.4999999999999999.
    # Deciding the status from the unrounded value would publish a report that
    # says "burn_rate: 0.5" next to "status: healthy" -- internally
    # contradictory, and the kind of thing that destroys trust in a dashboard.
    availability = round(max(0.0, 1.0 - (errors_total / requests_total)), 6)
    burn_rate = round((1.0 - availability) / slo.error_budget_ratio, 4)
    budget_remaining = round(max(0.0, 1.0 - burn_rate), 6)
    availability_met = availability >= slo.availability_target

    if latency_total > 0:
        latency_compliance: float | None = round(
            max(0.0, min(1.0, latency_good / latency_total)), 6
        )
        latency_met: bool | None = latency_compliance >= slo.latency_compliance_target
    else:
        latency_compliance = None
        latency_met = None

    report["availability"] = {
        "sli": availability,
        "objective": slo.availability_target,
        "met": availability_met,
        "error_budget_remaining": budget_remaining,
        "burn_rate": burn_rate,
        "errors": round(errors_total, 4),
    }
    report["latency"] = {
        "sli": latency_compliance,
        "objective": slo.latency_compliance_target,
        "met": latency_met,
        "threshold_ms": slo.latency_target_ms,
        "bucket_ms": round(slo.latency_bucket_seconds * 1000, 3),
    }

    if not availability_met or latency_met is False:
        report["status"] = "breached"
        report["summary"] = "At least one objective is below target; the error budget is exhausted."
    elif burn_rate >= 0.5:
        report["status"] = "at_risk"
        report["summary"] = "Objectives are met, but at least half of the error budget is spent."
    else:
        report["status"] = "healthy"
        report["summary"] = "All objectives met and the error budget is healthy."

    return report


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def evaluate_slo() -> dict[str, Any]:
    """Gather a snapshot from the best available source and evaluate it.

    Never raises. A monitoring endpoint that returns 500 because the monitoring
    system is down is worse than useless -- during an incident it is the one
    thing you need to be able to read. A failure to reach Prometheus degrades to
    the local registry and says so in the response.
    """
    slo = SLO.from_settings()
    source = "local_registry"
    window_source = "process_lifetime"
    snapshot: dict[str, float | None] | None = None

    prometheus_url = getattr(settings, "PROMETHEUS_URL", "")
    if prometheus_url:
        client = PrometheusClient(
            prometheus_url,
            timeout=float(getattr(settings, "PROMETHEUS_QUERY_TIMEOUT", 3.0)),
        )
        try:
            snapshot = client.snapshot(slo)
            source = "prometheus"
            window_source = slo.window
        except PrometheusUnavailable as exc:
            SLO_EVALUATION_FALLBACKS.labels(reason=type(exc).__name__).inc()
            logger.warning(
                "slo_prometheus_unavailable",
                extra={"prometheus_url": prometheus_url, "detail": str(exc)[:200]},
            )

    if snapshot is None:
        snapshot = _local_snapshot(slo.latency_bucket_seconds)

    report = compute_slo(
        snapshot=snapshot,
        slo=slo,
        source=source,
        window_source=window_source,
        service=settings.SERVICE_NAME,
    )
    _publish_gauges(report)
    return report


def get_slo_report(*, use_cache: bool = True) -> dict[str, Any]:
    """Cached variant used by the HTTP endpoint.

    Evaluating an SLO costs two round trips to Prometheus. The uptime monitor,
    Grafana and a curious human all hit the same endpoint, and nothing about the
    answer changes inside one scrape interval, so the result is cached for that
    long.

    ``LocMemCache`` is per process, so with N gunicorn workers each one keeps its
    own copy. That is fine here (the report is idempotent and read-only) and it is
    why this is not a shared cache: adding Redis to deduplicate a 15-second cache
    would cost more than it saves. See docs/architecture.md.
    """
    if not use_cache:
        return evaluate_slo()

    cached = cache.get(REPORT_CACHE_KEY)
    if cached is not None:
        return cached

    report = evaluate_slo()
    cache.set(REPORT_CACHE_KEY, report, timeout=int(getattr(settings, "SLO_CACHE_SECONDS", 15)))
    return report


def _publish_gauges(report: dict[str, Any]) -> None:
    """Mirror the report into Prometheus so SLO state can be graphed over time.

    Gauges are only written when there is something to write. Setting a latency
    gauge to 0 because no samples arrived would render as a total latency
    failure, which is the opposite of the truth.
    """
    availability = report.get("availability", {})
    latency = report.get("latency", {})

    if availability.get("sli") is not None:
        SLO_AVAILABILITY.set(availability["sli"])
        SLO_BURN_RATE.set(availability.get("burn_rate") or 0.0)
        SLO_ERROR_BUDGET.set(availability.get("error_budget_remaining") or 0.0)

    if latency.get("sli") is not None:
        SLO_LATENCY_COMPLIANCE.set(latency["sli"])

    status = report.get("status")
    if status == "breached":
        SLO_TARGET_MET.set(0)
    elif status in {"healthy", "at_risk"}:
        SLO_TARGET_MET.set(1)
    # "no_data" leaves the gauge untouched: a fresh process has not failed, it
    # just has nothing to say yet.

    SLO_LAST_EVALUATION.set(time.time())

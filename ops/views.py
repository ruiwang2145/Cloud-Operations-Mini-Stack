"""Operational HTTP surface.

The endpoints here exist to answer one question each, and the set is deliberately
small enough to keep in your head:

============================ ==================================================
``GET /healthz/``            Liveness. "Is this process alive?"
``GET /readyz/``             Readiness. "Should traffic be sent to it?"
``GET /version/``            Which build is answering?
``GET /metrics``             Prometheus scrape target.
``GET /api/slo/``            Are we meeting the objectives, and how fast is the
                             error budget burning?
``GET /boom/``               Fault injection (disabled unless explicitly on).
``GET /sentry-debug/``       Fault injection (disabled unless explicitly on).
============================ ==================================================

Liveness and readiness are separate because they have opposite failure
behaviours. A liveness probe failing means "restart this process". A readiness
probe failing means "stop sending it traffic, but do not kill it". If the
database is down, a single combined probe would cause the orchestrator to
restart every healthy worker in a loop -- turning a database outage into a
self-inflicted denial of service. That is the whole reason the distinction
exists, and it is why ``/healthz/`` deliberately does not touch the database.
"""
from __future__ import annotations

import logging
import time
from functools import wraps

from django.conf import settings
from django.db import connections
from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache

from .logging import get_request_id
from .metrics import READINESS_FAILURES
from .slo import get_slo_report

logger = logging.getLogger("app.probes")

# Captured at import, which for a WSGI worker is when the worker started. Used
# only for the uptime figure in /healthz/.
PROCESS_STARTED_AT = time.time()


def _json(payload, status: int = 200) -> JsonResponse:
    """JSON responses for probes are never cached.

    A cached readiness check is worse than no readiness check: it would report
    the state of the world as it was, and the orchestrator would keep sending
    traffic to a process whose database connection died ten minutes ago.
    """
    return JsonResponse(payload, status=status, json_dumps_params={"indent": 2})


# --------------------------------------------------------------------------- #
# Liveness
# --------------------------------------------------------------------------- #
@never_cache
def liveness(request):
    """Is the process able to serve requests at all?

    Intentionally trivial: it does no I/O. If this endpoint fails, the process is
    wedged and only a restart helps. Anything that can fail independently of the
    process (the database, a downstream API) belongs in readiness instead.
    """
    return _json(
        {
            "status": "ok",
            "service": settings.SERVICE_NAME,
            "version": settings.SERVICE_VERSION,
            "environment": settings.DEPLOY_ENV,
            "uptime_seconds": round(time.time() - PROCESS_STARTED_AT, 1),
        }
    )


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #
@never_cache
def readiness(request):
    """Should this instance receive traffic?

    Checks the dependencies a request actually needs. Returns 503 when any of
    them is unusable, which takes the instance out of the load balancer without
    killing it -- so it can recover on its own when the dependency comes back.
    """
    checks = {"database": _check_database()}
    ready = all(check["ok"] for check in checks.values())

    for name, check in checks.items():
        if not check["ok"]:
            READINESS_FAILURES.labels(dependency=name).inc()
            logger.warning(
                "readiness_check_failed",
                extra={
                    "dependency": name,
                    "detail": check.get("error"),
                    "latency_ms": check.get("latency_ms"),
                },
            )

    return _json(
        {
            "status": "ready" if ready else "not_ready",
            "checks": checks,
            "request_id": get_request_id(),
        },
        status=200 if ready else 503,
    )


def _check_database() -> dict:
    started = time.perf_counter()
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}


# --------------------------------------------------------------------------- #
# Version
# --------------------------------------------------------------------------- #
@never_cache
def version(request):
    return _json(
        {
            "service": settings.SERVICE_NAME,
            "version": settings.SERVICE_VERSION,
            "environment": settings.DEPLOY_ENV,
            "debug": settings.DEBUG,
            "fault_endpoints_enabled": settings.ENABLE_FAULT_ENDPOINTS,
        }
    )


# --------------------------------------------------------------------------- #
# SLO
# --------------------------------------------------------------------------- #
@never_cache
def slo_status(request):
    """A human- and machine-readable answer to "are we inside our budget?".

    Always returns 200, even when the SLO is breached. The endpoint reports a
    state; it is not itself a health check. Something that has to distinguish
    "the SLO is breached" from "the SLO endpoint is broken" needs those to be
    different response codes.
    """
    return _json(get_slo_report())


# --------------------------------------------------------------------------- #
# Fault injection
# --------------------------------------------------------------------------- #
def require_fault_endpoints(view):
    """Hide the fault-injection endpoints unless they are explicitly enabled.

    Returning 404 rather than 403 matters: a 403 confirms the endpoint exists and
    invites someone to keep trying. A 404 says nothing at all.
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not settings.ENABLE_FAULT_ENDPOINTS:
            raise Http404("Not found.")
        return view(request, *args, **kwargs)

    return wrapper


@never_cache
@require_fault_endpoints
def boom(request):
    """Raise a RuntimeError: proves error capture and alerting work end to end."""
    raise RuntimeError("deliberate failure injected by /boom/ for the incident drill")


@never_cache
@require_fault_endpoints
def sentry_debug(request):
    """Raise a ZeroDivisionError through a code path DRF also touches."""
    return _json({"unreachable": 1 / 0})

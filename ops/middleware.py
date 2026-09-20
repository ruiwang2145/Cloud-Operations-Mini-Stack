"""Request-scoped observability middleware.

Two middleware, each with a single job.

``RequestIdMiddleware``
    Correlates everything that happens because of one request. The id is taken
    from an inbound ``X-Request-ID`` when a proxy or the caller supplies one, and
    generated otherwise. It is then echoed back on the response, which is what
    turns "the user says it failed at 14:02" into a single log query.

``ObservabilityMiddleware``
    Emits one structured access log line and records the RED metrics, from a
    *single* timer. Two separate middleware measuring the same request would
    drift: the log would say 41 ms while the histogram recorded 43 ms, and the
    first person to notice would have to work out which one to trust.

Why the error handling looks the way it does
--------------------------------------------
Django wraps **every** middleware in ``convert_exception_to_response`` while it
builds the handler chain (``django/core/handlers/base.py``). The practical
consequence is that an exception raised in a view never crosses a middleware
boundary: by the time control returns to this middleware, Django has already
turned it into a 500 response. A naive ``try/except`` around
``self.get_response(request)`` therefore never fires, and the most interesting
requests in the system -- the ones that crashed -- would be missing from the
error metrics.

The exception is observed instead through Django's ``process_exception`` hook,
which the handler calls with the *raw* exception before generating the response.
Both observation points exist, so the request carries a flag
(``error_already_recorded``) to make sure one failure is counted exactly once.
"""
from __future__ import annotations

import logging
import sys
import time
from uuid import uuid4

from .endpoints import normalise_endpoint
from .exceptions import error_already_recorded, mark_error_recorded
from .logging import REQUEST_ID_HEADER, reset_request_id, set_request_id
from .metrics import ERRORS, REQUEST_LATENCY, REQUESTS, status_class

logger = logging.getLogger("app.access")

# A correlation id only has to be unique, not pretty. Truncating whatever the
# caller sent bounds the value so a malicious header cannot inflate every log
# line and every response header.
MAX_REQUEST_ID_LENGTH = 64


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = (request.META.get("HTTP_X_REQUEST_ID") or "").strip()
        request_id = incoming[:MAX_REQUEST_ID_LENGTH] or uuid4().hex[:16]

        request.request_id = request_id
        set_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            # The context variable belongs to this request only. Leaking it
            # would make the next log line on this thread lie about which
            # request it came from.
            reset_request_id()

        response[REQUEST_ID_HEADER] = request_id
        return response


class ObservabilityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        try:
            response = self.get_response(request)
        except Exception:
            # Defensive: Django's per-middleware exception wrapper means this
            # should be unreachable for anything raised below us. It exists so
            # that a failure inside this middleware's own layer is still logged
            # rather than vanishing.
            duration = time.perf_counter() - started
            endpoint = normalise_endpoint(request)
            ERRORS.labels(endpoint=endpoint, error_type="MiddlewareError").inc()
            self._record(request, endpoint, 500, duration)
            self._log(request, endpoint, 500, duration, exc_info=sys.exc_info())
            raise

        duration = time.perf_counter() - started
        endpoint = normalise_endpoint(request)
        self._record(request, endpoint, response.status_code, duration)

        if response.status_code >= 500 and not error_already_recorded(request):
            # A 5xx that did *not* come from an exception: a view that returned
            # an error response directly, or a DRF APIException that never left
            # the view. There is no exception type to attribute it to, so it is
            # labelled by status instead of being dropped.
            ERRORS.labels(endpoint=endpoint, error_type="Http5xx").inc()

        self._log(request, endpoint, response.status_code, duration)
        return response

    def process_exception(self, request, exception):
        """Called by Django with the raw exception, before the 500 is built.

        Returning ``None`` lets Django continue and produce the response it would
        have produced anyway -- this hook observes, it does not intervene.
        """
        endpoint = normalise_endpoint(request)
        error_type = type(exception).__name__

        ERRORS.labels(endpoint=endpoint, error_type=error_type).inc()
        mark_error_recorded(request)

        logger.error(
            "unhandled_exception",
            extra={
                "endpoint": endpoint,
                "error_type": error_type,
                "exception_message": str(exception)[:500],
                "request_id": getattr(request, "request_id", None),
            },
            exc_info=exception,
        )
        return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _record(request, endpoint: str, status: int, duration: float) -> None:
        REQUESTS.labels(
            method=request.method,
            endpoint=endpoint,
            status_class=status_class(status),
        ).inc()
        REQUEST_LATENCY.labels(method=request.method, endpoint=endpoint).observe(duration)

    def _log(self, request, endpoint: str, status: int, duration: float, exc_info=None) -> None:
        if status >= 500:
            level = logging.ERROR
        elif status >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO

        logger.log(
            level,
            "http_request",
            extra={
                "http": {
                    "method": request.method,
                    "endpoint": endpoint,
                    "path": request.path,
                    "status": status,
                },
                "duration_ms": round(duration * 1000, 3),
                "client_ip": _client_ip(request),
                "user_agent": (request.META.get("HTTP_USER_AGENT") or "")[:200],
                "user_id": getattr(getattr(request, "user", None), "pk", None),
                "request_id": getattr(request, "request_id", None),
            },
            exc_info=exc_info,
        )


def _client_ip(request) -> str:
    """Real client address.

    Behind a load balancer ``REMOTE_ADDR`` is the balancer, so every log line
    would claim the same source. ``X-Forwarded-For`` is a comma-separated chain
    (client, proxy1, proxy2...); the left-most entry is the original client. It
    is trivially spoofable and therefore treated as *diagnostic data only* --
    never as an authorisation input.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.META.get("REMOTE_ADDR") or "")[:64]


__all__ = ["RequestIdMiddleware", "ObservabilityMiddleware"]

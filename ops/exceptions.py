"""A single error contract for the whole API.

Two problems are solved here.

**Consistency.** Out of the box DRF returns three different shapes: a list for
validation errors, ``{"detail": ...}`` for permission errors, and whatever the
view happened to return for a 5xx. Every client then needs three code paths. The
handler below wraps everything in one envelope::

    {"error": {"type": "ValidationError", "status": 400,
               "detail": {...}, "request_id": "9f2c1a4b7e0d3856"}}

The ``request_id`` is the useful part: a user can paste it into a support
ticket, and it maps directly onto the access log line and the Sentry event for
that exact request.

**Observability.** A 4xx is the client's problem and gets a warning log. A 5xx is
ours and gets an error metric labelled with the exception type, so the
dashboard can distinguish "the database is timing out" from "we shipped a
``KeyError``".
"""
from __future__ import annotations

import logging

from rest_framework.views import exception_handler as drf_exception_handler

from .endpoints import normalise_endpoint
from .logging import get_request_id
from .metrics import ERRORS

logger = logging.getLogger("app.errors")

# Attribute used to mark a request whose failure has already been counted, so
# that two observers of the same failure do not both increment the counter.
# See `mark_error_recorded` for why this lives on the request and not the
# response.
ERROR_RECORDED_ATTR = "_app_error_recorded"


def mark_error_recorded(request) -> None:
    """Flag a request as already counted against ``app_errors_total``.

    A single failure is observed from two places: Django's ``process_exception``
    middleware hook (which sees the raw exception) and the response path in
    ``ObservabilityMiddleware`` (which sees the 500 that Django generates
    afterwards). Both are legitimate observation points and both would count the
    same failure.

    The flag goes on the *request*, not the response, because the two observers
    hold different response objects -- Django builds a brand new 500 response
    after ``process_exception`` returns, so a marker set on the original response
    would be lost. The request object, by contrast, is the same instance
    throughout the whole cycle.
    """
    target = getattr(request, "_request", request)  # unwrap DRF's Request wrapper
    setattr(target, ERROR_RECORDED_ATTR, True)


def error_already_recorded(request) -> bool:
    return bool(getattr(request, ERROR_RECORDED_ATTR, False))


def structured_exception_handler(exc, context):
    """DRF ``EXCEPTION_HANDLER``."""
    response = drf_exception_handler(exc, context)

    request = context.get("request")
    endpoint = normalise_endpoint(request) if request is not None else "unknown"
    request_id = get_request_id()
    error_type = type(exc).__name__

    if response is None:
        # Not an APIException: DRF does not know what to do with it, so it
        # returns None and Django turns it into a 500. Record it and hand it
        # back -- swallowing it here would hide the error entirely.
        ERRORS.labels(endpoint=endpoint, error_type=error_type).inc()
        if request is not None:
            mark_error_recorded(request)
        logger.exception(
            "api_unhandled_exception",
            extra={"endpoint": endpoint, "error_type": error_type, "request_id": request_id},
        )
        return None

    if response.status_code >= 500:
        # DRF turns APIException subclasses into responses *inside* the view, so
        # Django's process_exception hook never fires for them. This is the only
        # place that sees them.
        ERRORS.labels(endpoint=endpoint, error_type=error_type).inc()
        if request is not None:
            mark_error_recorded(request)
        logger.error(
            "api_server_error",
            extra={
                "endpoint": endpoint,
                "status": response.status_code,
                "error_type": error_type,
                "detail": _safe_detail(response.data),
                "request_id": request_id,
            },
        )
    else:
        # A 4xx is the caller's mistake, not a failure of the service, so it is
        # logged but never counted against availability.
        logger.warning(
            "api_client_error",
            extra={
                "endpoint": endpoint,
                "status": response.status_code,
                "error_type": error_type,
                "detail": _safe_detail(response.data),
                "request_id": request_id,
            },
        )

    response.data = {
        "error": {
            "type": error_type,
            "status": response.status_code,
            "detail": response.data,
            "request_id": request_id,
        }
    }
    return response


def _safe_detail(data, limit: int = 500) -> object:
    """Keep error details small enough to be useful in a log line.

    A validation error on a bulk endpoint can carry kilobytes of field errors.
    Truncating here keeps one bad request from filling the log.
    """
    if isinstance(data, (str, int, float, bool)) or data is None:
        return data
    text = str(data)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"

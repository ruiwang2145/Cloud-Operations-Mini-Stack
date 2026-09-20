"""Sentry event filtering.

Imported by ``my_cloudapp.settings`` at module import time, so this file must not
import Django settings (circular) and must not have side effects.
"""
from __future__ import annotations

from typing import Any

# Paths whose failures are already reported by the uptime monitor and by the
# metrics, and which fire on a fixed schedule forever. Sending them to Sentry
# costs quota and buries the errors that matter.
PROBE_PATHS = ("/healthz/", "/readyz/", "/metrics")


def drop_probe_events(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """``before_send`` hook: return ``None`` to discard the event.

    Only *successful-or-expected* probe traffic is dropped. A 5xx from a probe
    is a real outage and must be reported, which is why the status code is part
    of the condition rather than the path alone.
    """
    request = event.get("request") or {}
    url = str(request.get("url") or "")
    if not any(path in url for path in PROBE_PATHS):
        return event

    status = _status_code(event)
    if status is not None and status >= 500:
        return event

    return None


def _status_code(event: dict[str, Any]) -> int | None:
    """Best-effort extraction of the HTTP status from a Sentry event.

    The Django integration puts it in ``contexts.response.status_code``; the tag
    is checked first because a project can override the context while the tag
    survives.
    """
    candidates: list[Any] = [
        (event.get("tags") or {}).get("status_code"),
        ((event.get("contexts") or {}).get("response") or {}).get("status_code"),
    ]
    for raw in candidates:
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None

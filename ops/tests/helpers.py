"""Helpers for asserting on Prometheus metrics.

Counters in ``prometheus_client`` are process-global and monotonically
increasing, so a test can never assert an absolute value -- it would pass on the
first run and fail on the second. Every assertion in this suite therefore
compares a *delta* around the action under test, which is also how you have to
read these metrics in production.
"""
from __future__ import annotations

from typing import Any

from prometheus_client import REGISTRY


def metric_value(name: str, **labels: Any) -> float:
    """Current value of a single sample, or 0.0 when it does not exist yet.

    A missing sample is treated as zero on purpose: before the first request the
    series does not exist at all, and "zero requests" is what that means.
    """
    wanted = {key: str(value) for key, value in labels.items()}
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name != name:
                continue
            if all(sample.labels.get(key) == value for key, value in wanted.items()):
                return sample.value
    return 0.0


def metric_labels(name: str) -> set[tuple[str, ...]]:
    """Every distinct label tuple currently present for a metric."""
    found: set[tuple[str, ...]] = set()
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == name:
                found.add(tuple(sorted(sample.labels.items())))
    return found


def endpoint_labels(name: str = "app_http_requests_total") -> set[str]:
    """The distinct ``endpoint`` label values recorded so far."""
    values: set[str] = set()
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == name and "endpoint" in sample.labels:
                values.add(sample.labels["endpoint"])
    return values

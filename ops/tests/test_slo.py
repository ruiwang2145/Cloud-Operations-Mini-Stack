"""Tests for the SLO arithmetic.

``compute_slo`` is a pure function of numbers, so these tests need no database,
no HTTP and no mocking. That is the point of separating it from the code that
fetches the numbers: the part with the interesting edge cases is the part that is
trivial to test.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ops.slo import SLO, compute_slo

SLO_995 = SLO(
    availability_target=0.995,
    latency_target_ms=300,
    latency_compliance_target=0.99,
    window="30d",
)


def snapshot(requests=1000.0, errors=0.0, latency_total=1000.0, latency_good=1000.0):
    return {
        "requests_total": requests,
        "errors_total": errors,
        "latency_total": latency_total,
        "latency_good": latency_good,
    }


def evaluate(**kwargs):
    return compute_slo(
        snapshot=snapshot(**kwargs),
        slo=SLO_995,
        source="prometheus",
        window_source="30d",
        service="test-service",
        generated_at=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
    )


class TestAvailability:
    def test_perfect_availability_spends_no_budget(self):
        report = evaluate()
        assert report["availability"]["sli"] == 1.0
        assert report["availability"]["met"] is True
        assert report["availability"]["burn_rate"] == 0.0
        assert report["availability"]["error_budget_remaining"] == 1.0
        assert report["status"] == "healthy"

    def test_exactly_at_target_is_still_met(self):
        # 0.5% of 1000 requests = 5 failures. The objective is "at least 99.5%",
        # so landing exactly on it passes.
        report = evaluate(errors=5.0)
        assert report["availability"]["sli"] == 0.995
        assert report["availability"]["met"] is True

    def test_one_error_over_target_breaches(self):
        report = evaluate(errors=6.0)
        assert report["availability"]["sli"] == 0.994
        assert report["availability"]["met"] is False
        assert report["status"] == "breached"
        # The budget is fully spent; it never goes negative.
        assert report["availability"]["error_budget_remaining"] == 0.0

    def test_burn_rate_of_one_means_the_budget_lasts_exactly_the_window(self):
        # Spending exactly the permitted 0.5% burns the budget at rate 1.0.
        report = evaluate(errors=5.0)
        assert report["availability"]["burn_rate"] == pytest.approx(1.0)

    def test_half_the_budget_spent_is_at_risk(self):
        # 0.25% error rate against a 0.5% budget -> burn rate 0.5.
        report = evaluate(errors=2.5)
        assert report["availability"]["burn_rate"] == pytest.approx(0.5)
        assert report["availability"]["error_budget_remaining"] == pytest.approx(0.5)
        assert report["status"] == "at_risk"

    def test_burn_rate_scales_linearly(self):
        report = evaluate(errors=20.0)  # 2% error rate = 4x the budget
        assert report["availability"]["burn_rate"] == pytest.approx(4.0)


class TestLatency:
    def test_all_requests_fast_enough_meets_the_objective(self):
        report = evaluate()
        assert report["latency"]["sli"] == 1.0
        assert report["latency"]["met"] is True

    def test_99_percent_compliance_is_met(self):
        report = evaluate(latency_total=1000.0, latency_good=990.0)
        assert report["latency"]["sli"] == pytest.approx(0.99)
        assert report["latency"]["met"] is True

    def test_98_9_percent_compliance_breaches(self):
        report = evaluate(latency_total=1000.0, latency_good=989.0)
        assert report["latency"]["met"] is False
        assert report["status"] == "breached"

    def test_latency_breach_alone_is_enough_to_breach(self):
        report = evaluate(latency_total=1000.0, latency_good=500.0)
        assert report["availability"]["met"] is True
        assert report["status"] == "breached"

    def test_objective_is_reported_against_the_bucket_actually_used(self):
        # 300 ms is an exact bucket boundary in ops/metrics.py, so the number the
        # SLI is computed against and the number in the objective agree.
        report = evaluate()
        assert report["latency"]["threshold_ms"] == 300
        assert report["latency"]["bucket_ms"] == 300.0

    def test_bucket_falls_back_to_nearest_boundary_above_the_objective(self):
        slo = SLO(0.995, latency_target_ms=350, latency_compliance_target=0.99, window="30d")
        # 350 ms is not a bucket, so the SLI is measured at 500 ms and says so.
        assert slo.latency_bucket_seconds == 0.5


class TestNoData:
    """The case that a naive implementation gets wrong by reporting 100%."""

    def test_zero_traffic_reports_no_data_not_perfect_availability(self):
        report = evaluate(requests=0.0, errors=0.0, latency_total=0.0, latency_good=0.0)
        assert report["status"] == "no_data"
        assert report["availability"]["sli"] is None
        assert report["availability"]["met"] is None
        assert report["availability"]["error_budget_remaining"] is None
        assert report["latency"]["sli"] is None

    def test_traffic_without_latency_samples_still_reports_availability(self):
        report = evaluate(latency_total=0.0, latency_good=0.0)
        assert report["availability"]["sli"] == 1.0
        assert report["latency"]["sli"] is None
        assert report["latency"]["met"] is None

    def test_missing_snapshot_keys_do_not_raise(self):
        report = compute_slo(
            snapshot={},
            slo=SLO_995,
            source="local_registry",
            window_source="process_lifetime",
        )
        assert report["status"] == "no_data"

    def test_none_values_do_not_raise(self):
        # This is the shape Prometheus returns when a series has no samples yet.
        report = compute_slo(
            snapshot={
                "requests_total": None,
                "errors_total": None,
                "latency_total": None,
                "latency_good": None,
            },
            slo=SLO_995,
            source="prometheus",
            window_source="30d",
        )
        assert report["status"] == "no_data"


class TestConfiguration:
    def test_error_budget_ratio_derives_from_the_objective(self):
        assert SLO_995.error_budget_ratio == pytest.approx(0.005)

    def test_a_100_percent_objective_does_not_divide_by_zero(self):
        slo = SLO(1.0, 300, 0.99, "30d")
        report = compute_slo(
            snapshot=snapshot(errors=0.0),
            slo=slo,
            source="prometheus",
            window_source="30d",
        )
        assert report["availability"]["burn_rate"] == 0.0

        breached = compute_slo(
            snapshot=snapshot(errors=1.0),
            slo=slo,
            source="prometheus",
            window_source="30d",
        )
        assert breached["availability"]["met"] is False
        assert breached["availability"]["error_budget_remaining"] == 0.0

    def test_report_carries_the_provenance_of_its_window(self):
        # A consumer must be able to tell a 30-day Prometheus answer from a
        # five-minute in-process one without guessing.
        report = compute_slo(
            snapshot=snapshot(),
            slo=SLO_995,
            source="local_registry",
            window_source="process_lifetime",
        )
        assert report["source"] == "local_registry"
        assert report["window_source"] == "process_lifetime"
        assert report["window"] == "30d"

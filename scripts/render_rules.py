#!/usr/bin/env python
"""Render ``monitoring/prometheus/alert_rules.yml`` from the SLO definition.

Why generate a config file instead of writing it by hand
-------------------------------------------------------
The availability objective appears in three places:

1. ``my_cloudapp/settings.py`` -- what the application reports on ``/api/slo/``.
2. ``docs/SLO.md`` -- what the team agreed to.
3. ``monitoring/prometheus/alert_rules.yml`` -- what actually pages someone.

The third is the one that matters at 03:00, and it is the one most likely to be
stale: someone edits the objective in settings, the dashboard and the API report
the new number, and the alert keeps firing against the old one. Nobody notices
until an incident where the alert stays silent.

Generating the rules from the settings removes the possibility. ``--check`` turns
drift into a failing build, which is how it stays true:

    python scripts/render_rules.py            # write the file
    python scripts/render_rules.py --check    # exit 1 if it is out of date

Run through Django's settings loader so environment overrides are honoured --
``SLO_AVAILABILITY_TARGET=0.999 python scripts/render_rules.py`` renders rules
for the objective you are actually about to deploy.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "monitoring" / "prometheus" / "alert_rules.yml"

# Running a script from `scripts/` puts `scripts/` on sys.path, not the project
# root, so `import ops` would fail. Adding the root explicitly is what makes this
# script runnable as `python scripts/render_rules.py` from anywhere.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --------------------------------------------------------------------------- #
# Alerting policy
#
# These multipliers are *not* part of the SLO definition -- they are how loudly
# we choose to react to spending the budget. They follow the multi-window
# burn-rate approach from the Google SRE Workbook:
#
#   14.4x over 1h -> 2% of a 30-day budget gone in an hour. Page: this is a fast
#                    burn and the budget will not survive the day.
#    6.0x over 6h -> 5% of the budget gone in six hours. Ticket: real, but
#                    survivable, and worth a human looking during working hours.
#
# Two windows rather than one, because a single short window pages on every
# transient blip, and a single long window stays quiet during a fast outage that
# is burning the budget in minutes.
# --------------------------------------------------------------------------- #
FAST_BURN_MULTIPLIER = 14.4
FAST_BURN_WINDOW = "1h"
SLOW_BURN_MULTIPLIER = 6.0
SLOW_BURN_WINDOW = "6h"

# A separate, absolute error-rate alert. Burn rate answers "will we run out of
# budget?"; this answers "is something badly broken right now?", and that second
# question needs an answer even for a service with a generous objective.
ACUTE_ERROR_RATE_THRESHOLD = 0.05
ACUTE_ERROR_RATE_WINDOW = "5m"

LATENCY_ALERT_WINDOW = "10m"

BUDGET_WINDOW_DAYS = 30

# --------------------------------------------------------------------------- #
# Prometheus' own alert-annotation templating uses `{{ ... }}`.
#
# Interpolating those inside an f-string means quadrupling every brace, which is
# unreadable and easy to get subtly wrong. Naming them as constants keeps the
# template below plain and lets the placeholders survive f-string interpolation
# untouched.
# --------------------------------------------------------------------------- #
T_INSTANCE = "{{ $labels.instance }}"
T_DEPENDENCY = "{{ $labels.dependency }}"
T_REASON = "{{ $labels.reason }}"
T_VALUE_HUMAN = "{{ $value | humanize }}"
T_VALUE_PERCENT = "{{ $value | humanizePercentage }}"

HEADER = """\
# =============================================================================
# GENERATED FILE -- DO NOT EDIT BY HAND
#
#   Regenerate with:  python scripts/render_rules.py
#   Verify with:      python scripts/render_rules.py --check
#
# The objectives below are rendered from my_cloudapp/settings.py, so the alerts,
# the /api/slo/ endpoint and docs/SLO.md cannot drift apart.
#
# There is no Alertmanager in this stack, so firing alerts are visible in the
# Prometheus UI at /alerts. Routing them to a real destination
# (Alertmanager -> PagerDuty/Slack) is a deployment concern and would live next
# to this file. See docs/SLO.md.
# =============================================================================
"""


def render() -> str:
    """Build the alert rules file content."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "my_cloudapp.settings")
    # The settings module deliberately refuses to load without a signing key.
    # Rendering an alert file does not need a real one.
    os.environ.setdefault("ALLOW_INSECURE_SECRET_KEY", "1")
    os.environ.setdefault("LOG_TO_FILE", "False")

    import django

    django.setup()


    from ops.endpoints import PROBE_ENDPOINTS
    from ops.slo import SLO

    slo = SLO.from_settings()
    availability = slo.availability_target
    latency_ms = slo.latency_target_ms
    latency_compliance = slo.latency_compliance_target

    budget = slo.error_budget_ratio
    # The latency SLI is measured at the histogram bucket, which is not always the
    # same number as the objective -- see SLO.latency_bucket_seconds.
    bucket_ms = slo.latency_bucket_seconds * 1000

    fast_burn_budget_per_hour = FAST_BURN_MULTIPLIER / (BUDGET_WINDOW_DAYS * 24) * 100

    probe = "|".join(sorted(PROBE_ENDPOINTS))
    not_probe = f'endpoint!~"{probe}"'

    def error_ratio(window: str) -> str:
        return (
            f'sum(rate(app_http_requests_total{{status_class="5xx",{not_probe}}}[{window}]))\n'
            f"            /\n"
            f"            sum(rate(app_http_requests_total{{{not_probe}}}[{window}]))"
        )

    def burn_rate(window: str) -> str:
        return (
            f'sum(rate(app_http_requests_total{{status_class="5xx",{not_probe}}}[{window}]))\n'
            f"            /\n"
            f"            sum(rate(app_http_requests_total{{{not_probe}}}[{window}]))\n"
            f"            / {budget:g}"
        )

    latency_compliance_expr = (
        f'sum(rate(app_http_request_duration_seconds_bucket'
        f'{{le="{bucket_ms / 1000:g}",{not_probe}}}[{LATENCY_ALERT_WINDOW}]))\n'
        f"          /\n"
        f"          sum(rate(app_http_request_duration_seconds_count"
        f"{{{not_probe}}}[{LATENCY_ALERT_WINDOW}]))"
    )

    return f"""{HEADER}
groups:
  - name: availability
    interval: 30s
    rules:
      # ---------------------------------------------------------------------
      # The service is not answering at all.
      #
      # `up` is produced by Prometheus itself, so this rule keeps working when
      # the application is too broken to export metrics -- which is exactly the
      # case it exists to catch.
      # ---------------------------------------------------------------------
      - alert: ServiceDown
        expr: up{{job="cloud-ops-mini-stack"}} == 0
        for: 2m
        labels:
          severity: critical
          slo: availability
        annotations:
          summary: "cloud-ops-mini-stack is not being scraped"
          description: >-
            Prometheus has failed to scrape {T_INSTANCE} for 2 minutes. Either the
            process is gone or /metrics is not answering.
          runbook: "docs/runbooks/service-down.md"

      # ---------------------------------------------------------------------
      # Fast burn: {FAST_BURN_MULTIPLIER}x the sustainable rate over {FAST_BURN_WINDOW}.
      # At this rate about {fast_burn_budget_per_hour:.1f}% of a {BUDGET_WINDOW_DAYS}-day budget is
      # consumed per hour, so the budget does not survive the day.
      # ---------------------------------------------------------------------
      - alert: AvailabilityBudgetBurnFast
        expr: |
          {burn_rate(FAST_BURN_WINDOW)} > {FAST_BURN_MULTIPLIER}
        for: 2m
        labels:
          severity: critical
          slo: availability
        annotations:
          summary: "Availability error budget burning {FAST_BURN_MULTIPLIER}x too fast"
          description: >-
            Over the last {FAST_BURN_WINDOW} the 5xx ratio is {T_VALUE_HUMAN}x the rate the
            {availability:.3%} objective allows. Check
            app_slo_error_budget_remaining_ratio for what is left.
          runbook: "docs/runbooks/high-error-rate.md"

      # ---------------------------------------------------------------------
      # Slow burn: {SLOW_BURN_MULTIPLIER}x over {SLOW_BURN_WINDOW}. Real, survivable, and worth
      # a human during working hours.
      # ---------------------------------------------------------------------
      - alert: AvailabilityBudgetBurnSlow
        expr: |
          {burn_rate(SLOW_BURN_WINDOW)} > {SLOW_BURN_MULTIPLIER}
        for: 30m
        labels:
          severity: warning
          slo: availability
        annotations:
          summary: "Availability error budget burning {SLOW_BURN_MULTIPLIER}x too fast"
          description: >-
            Sustained over {SLOW_BURN_WINDOW}. Not an outage yet, but at this rate the
            {availability:.3%} objective is missed before the end of the window.
          runbook: "docs/runbooks/high-error-rate.md"

      # ---------------------------------------------------------------------
      # Acute error rate, independent of the budget. A service can be inside its
      # monthly budget and still be broken right now.
      # ---------------------------------------------------------------------
      - alert: HighErrorRate
        expr: |
          {error_ratio(ACUTE_ERROR_RATE_WINDOW)} > {ACUTE_ERROR_RATE_THRESHOLD}
        for: 5m
        labels:
          severity: critical
          slo: availability
        annotations:
          summary: "More than {ACUTE_ERROR_RATE_THRESHOLD:.0%} of requests are failing"
          description: >-
            The 5xx ratio over {ACUTE_ERROR_RATE_WINDOW} is {T_VALUE_PERCENT}.
          runbook: "docs/runbooks/high-error-rate.md"

      # ---------------------------------------------------------------------
      # The error budget is gone. This is the point at which the agreed policy
      # says feature work stops and reliability work starts.
      # ---------------------------------------------------------------------
      - alert: ErrorBudgetExhausted
        expr: app_slo_error_budget_remaining_ratio <= 0
        for: 10m
        labels:
          severity: warning
          slo: availability
        annotations:
          summary: "Availability error budget exhausted"
          description: >-
            Measured availability over the evaluation window is below the
            {availability:.3%} objective. See docs/SLO.md for the budget policy.
          runbook: "docs/runbooks/high-error-rate.md"

  - name: latency
    interval: 30s
    rules:
      # ---------------------------------------------------------------------
      # Latency objective: {latency_compliance:.1%} of requests under {latency_ms} ms.
      #
      # Measured against the {bucket_ms:g} ms histogram bucket, the smallest bucket
      # that is not below the objective.
      # ---------------------------------------------------------------------
      - alert: LatencyObjectiveBreach
        expr: |
          {latency_compliance_expr} < {latency_compliance}
        for: 10m
        labels:
          severity: warning
          slo: latency
        annotations:
          summary: "Fewer than {latency_compliance:.1%} of requests meet the {latency_ms} ms objective"
          description: >-
            Compliance over the last {LATENCY_ALERT_WINDOW} is {T_VALUE_PERCENT}. Check the
            per-endpoint breakdown before assuming the database is at fault.
          runbook: "docs/runbooks/high-latency.md"

  - name: operations
    interval: 30s
    rules:
      # ---------------------------------------------------------------------
      # A readiness probe failed. The instance took itself out of the load
      # balancer, so no user saw an error -- but a dependency is unhealthy and
      # the next failure may not be absorbed.
      # ---------------------------------------------------------------------
      - alert: ReadinessCheckFailing
        expr: increase(app_readiness_failures_total[5m]) > 0
        for: 0m
        labels:
          severity: warning
          slo: operations
        annotations:
          summary: "Readiness probe failing for dependency {T_DEPENDENCY}"
          description: >-
            {T_DEPENDENCY} failed {T_VALUE_HUMAN} readiness check(s) in the last
            5 minutes.
          runbook: "docs/runbooks/database-unreachable.md"

      # ---------------------------------------------------------------------
      # The SLO reporter could not reach Prometheus and fell back to the
      # in-process registry. Monitoring has become partially blind: the report
      # is still served, but over the lifetime of the process instead of the
      # configured window.
      # ---------------------------------------------------------------------
      - alert: SloReporterDegraded
        expr: increase(app_slo_evaluation_fallbacks_total[15m]) > 0
        for: 0m
        labels:
          severity: warning
          slo: operations
        annotations:
          summary: "SLO reporter cannot query Prometheus"
          description: >-
            {T_VALUE_HUMAN} evaluation(s) fell back to the local registry
            ({T_REASON}). /api/slo/ is reporting process-lifetime numbers, not the
            configured window.
          runbook: "docs/runbooks/metrics-and-logs.md"

      # ---------------------------------------------------------------------
      # The service is reachable but nobody is using it. Usually a broken load
      # balancer or a bad routing deploy rather than a quiet day.
      # ---------------------------------------------------------------------
      - alert: NoTraffic
        expr: |
          sum(rate(app_http_requests_total{{endpoint!~"{probe}"}}[15m])) == 0
        for: 30m
        labels:
          severity: warning
          slo: operations
        annotations:
          summary: "No application traffic for 30 minutes"
          description: >-
            Probe endpoints are excluded, so this is real user traffic that has
            stopped -- not a monitoring artefact.
          runbook: "docs/runbooks/service-down.md"
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed file differs from the rendered output",
    )
    args = parser.parse_args()

    rendered = render()

    # Fail loudly if we generated something that is not valid YAML. A malformed
    # alert file makes Prometheus refuse to start, which is a spectacular way to
    # lose monitoring while changing monitoring.
    try:
        import yaml

        parsed = yaml.safe_load(rendered)
        assert isinstance(parsed, dict) and "groups" in parsed
        rule_count = sum(len(group["rules"]) for group in parsed["groups"])
    except ImportError:
        rule_count = -1
        print("warning: PyYAML not installed, skipping YAML validation", file=sys.stderr)

    if args.check:
        if not OUTPUT_PATH.exists():
            print(f"ERROR: {OUTPUT_PATH} does not exist", file=sys.stderr)
            return 1
        # Read without newline translation so the comparison is byte-exact.
        # `Path.read_text` is not used here because its `newline` parameter only
        # exists from Python 3.13, and this project supports 3.10+.
        with OUTPUT_PATH.open("r", encoding="utf-8", newline="") as handle:
            current = handle.read()
        if current != rendered:
            print(
                "ERROR: alert_rules.yml is out of date with the SLO definition.\n"
                "       Run: python scripts/render_rules.py",
                file=sys.stderr,
            )
            return 1
        print("alert_rules.yml is up to date")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # `newline="\n"` is not optional. `Path.write_text` translates "\n" to
    # `os.linesep`, so on Windows this would write CRLF while the committed file
    # (normalised by .gitattributes) is LF -- and the drift check would then fail
    # on the machine that generated it. Writing LF explicitly makes the output
    # byte-identical on every platform.
    OUTPUT_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    suffix = f", {rule_count} rules validated" if rule_count >= 0 else ""
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

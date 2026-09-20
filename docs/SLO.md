# Service Level Objectives

The contract this service is measured against, and the policy that applies when
it is missed.

## Why an SLO at all

"99.5% available" is not a marketing number. It is an *engineering decision* with
consequences, and writing it down is what makes those consequences arguable:

- **It defines "broken".** Without a target, every failure is equally alarming and
  the team either panics constantly or ignores everything.
- **It converts reliability into a budget.** 99.5% availability permits 0.5% of
  requests to fail. That is a quantity, and quantities can be spent.
- **It makes the trade-off explicit.** "Should we ship this feature or fix that
  flaky test?" has no answer without a budget. With one, the answer is arithmetic.
- **100% is the wrong target.** It costs more than it is worth and it is
  unachievable, so a target of 100% is really a target of "we will always be
  failing". Choosing 99.5% is a statement that some failures are acceptable —
  which is what lets you sleep.

## The objectives

| | Objective | Window | Source of truth |
|---|---|---|---|
| **Availability** | 99.5% of requests succeed | 30 days | `SLO_AVAILABILITY_TARGET` |
| **Latency compliance** | 99% of requests complete in under 300 ms | 30 days | `SLO_LATENCY_TARGET_MS`, `SLO_LATENCY_COMPLIANCE_TARGET` |

Both are set in `my_cloudapp/settings.py` and can be overridden by environment
variable. The Prometheus alert rules are **generated** from those values by
`scripts/render_rules.py`, and `ops/tests/test_alert_rules.py` fails the build if
the committed rules drift from the definition. There is one place to change an
objective, and three artefacts follow.

## Service Level Indicators

An SLI is the *measurement*. It has to be something the service can compute from
its own telemetry, without asking a human.

### Availability

```
availability = 1 - (5xx responses / total responses)
```

Measured from `app_http_requests_total`, which carries a `status_class` label.

**4xx does not count as an error.** A rejected write is the caller's mistake — a
missing token, a malformed payload — and the service did exactly what it should.
Counting it would make the SLI a measure of who is calling, not of whether the
service works. The one exception is a 4xx caused by the service's own bug, which
is a bug to fix rather than a metric to redefine.

**Probe traffic is excluded.** `/healthz/`, `/readyz/`, `/metrics` and `/version/`
are requested by monitoring on a fixed schedule. Counting them would let a probe
loop that never fails drag the measured error rate towards zero regardless of what
users experienced — the monitoring system flattering itself. The exclusion is
applied in three places that must agree: `ops/slo.py`, the alert rules, and the
Grafana dashboards. `ops/tests/test_dashboards.py` asserts it for the dashboards.

### Latency compliance

```
latency_compliance = requests faster than 300 ms / total requests
```

Measured from `app_http_request_duration_seconds_bucket{le="0.3"}`.

**Why a compliance ratio and not p95.** "95% of requests under 300 ms" and "99% of
requests under 300 ms" sound similar and behave very differently at the boundary.
A compliance ratio is also directly computable from a histogram bucket, whereas a
percentile requires `histogram_quantile` interpolation — accurate in the middle of
a bucket, approximate at the edges. Since the objective is 300 ms, 300 ms is a
histogram bucket boundary (`ops/metrics.py`), so the SLI is an exact lookup rather
than an estimate.

**Why the threshold is enforced at the bucket.** If the objective is changed to a
value that is not a bucket boundary, the SLI is measured at the next boundary up
and the response reports which one was used (`latency.bucket_ms` in
`/api/slo/`). Silently measuring against a threshold nobody chose is worse than
being slightly conservative.

## Error budget

```
error_budget = 1 - objective = 0.5% of requests over 30 days
```

**Burn rate** is how fast the budget is being spent:

```
burn_rate = measured_error_rate / permitted_error_rate
```

| Burn rate | Meaning |
|---|---|
| 0 | Nothing failing |
| 1.0 | Exactly on target — the budget lasts precisely the window |
| 2.0 | Budget gone in half the window |
| 14.4 | Budget gone in about two days |

14.4 is not arbitrary: `1 / (30 days × 24 h) × 14.4` ≈ 2% of the budget per hour.
That is the threshold from the Google SRE Workbook for paging on a fast burn, and
it is what `AvailabilityBudgetBurnFast` uses.

## Multi-window alerting

Two burn-rate alerts, not one:

| Alert | Window | Multiplier | Severity | Rationale |
|---|---|---|---|---|
| `AvailabilityBudgetBurnFast` | 1 h | 14.4× | critical | Fast burn. The budget does not survive the day. |
| `AvailabilityBudgetBurnSlow` | 6 h | 6× | warning | Slow burn. Real, survivable, worth a human during working hours. |

A single short window pages on every transient blip. A single long window stays
quiet through a fast outage that burns the budget in minutes. Both together
distinguish "something just broke badly" from "something has been slightly wrong
all day", which are different problems with different responses.

`HighErrorRate` exists separately, on an absolute 5% threshold. Burn rate answers
"will we run out of budget?"; this answers "is something badly broken right now?"
— and the second question deserves an answer even for a service with a very
generous objective.

## Budget policy

What happens when the budget is spent. This is the part that makes an SLO real;
without it, the number is decoration.

| Budget consumed | Policy |
|---|---|
| **< 50%** | Normal. Ship features. |
| **50–100%** | Elevated risk. New work needs a reliability justification; review whether a recent change is responsible. |
| **100%** | **Feature work stops.** The team works on reliability until the budget is restored or the window rolls over. |

The last line is the whole point. It converts "we should probably do something
about reliability" — which loses every prioritisation argument against a shipping
deadline — into a rule that does not need to win an argument.

**A single incident may exhaust the budget.** That is expected and it is not a
reason to change the objective. It is a reason to fix the cause.

## Measuring the window

Two data sources, in order of preference:

1. **Prometheus** (`PROMETHEUS_URL` set). The only source that holds enough
   history for a 30-day window, and the only one that keeps working when the
   application is down — which is when you most need it.
2. **The in-process registry** (fallback). The window is then the lifetime of the
   process. `/api/slo/` reports this honestly in `window_source` rather than
   presenting a five-minute-old process as if it had a month of history.

The fallback exists because a monitoring endpoint that returns 500 when monitoring
is down is worse than useless. It is a *degradation*, not an equivalence — the
`SloReporterDegraded` alert fires when it is in use.

## Checking the current state

```bash
curl -sS http://localhost:8000/api/slo/ | python -m json.tool
```

```json
{
  "window": "30d",
  "window_source": "30d",
  "source": "prometheus",
  "objectives": {"availability": 0.995, "latency_threshold_ms": 300, "latency_compliance": 0.99},
  "availability": {
    "sli": 0.9991,
    "objective": 0.995,
    "met": true,
    "error_budget_remaining": 0.82,
    "burn_rate": 0.18
  },
  "latency": {"sli": 0.994, "objective": 0.99, "met": true, "threshold_ms": 300, "bucket_ms": 300.0},
  "status": "healthy"
}
```

`status` is one of `healthy`, `at_risk` (objectives met but ≥50% of the budget
spent), `breached`, or `no_data`. `no_data` is not success — it means the window
contained no traffic, which for a live service means the measurement is broken.

## Changing an objective

```bash
# 1. Change it in one place.
$EDITOR my_cloudapp/settings.py     # or set the environment variable

# 2. Regenerate the alert rules so the alerts follow.
python scripts/render_rules.py

# 3. Confirm nothing drifted.
python scripts/render_rules.py --check
python -m pytest ops/tests/test_alert_rules.py
```

If step 2 produces a diff, the alerts *were* out of date. That is the mechanism
working.

## What this SLO does not cover

- **Correctness.** A request that returns 200 with the wrong data is 100%
  available. SLOs measure availability, not truth.
- **Data durability.** Not covered here. A backup-and-restore objective would need
  a periodic restore drill, which this project does not have.
- **Per-endpoint objectives.** The objective is service-wide, so a broken
  `/api/tasks/stats/` is diluted by healthy traffic on `/api/tasks/`. Per-endpoint
  SLOs are the next step and are deliberately not attempted yet: two objectives
  that nobody has tuned are worse than one that is understood.

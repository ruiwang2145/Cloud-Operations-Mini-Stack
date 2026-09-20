# Incident drill log

Evidence that the detection path works, recorded from drills that were actually
run rather than from the design intent.

## Why drill

A monitoring stack that has never observed a failure is a hypothesis. Nobody knows
whether the alert fires, whether the exception reaches the error counter, whether
the log line carries the request id, or how long any of it takes — until something
breaks in production, which is the worst possible moment to find out.

A drill makes one failure happen on purpose and measures the path from cause to
evidence:

```
inject  →  detect in the metrics  →  correlate in the logs  →  confirm in the SLO
```

## Running a drill

```bash
# Fault endpoints must be enabled for the injection drills.
#   local:  ENABLE_FAULT_ENDPOINTS=True in .env
#   docker: already set in docker-compose.yml
export ENABLE_FAULT_ENDPOINTS=True

python scripts/run_drill.py --drill unhandled-exception
python scripts/run_drill.py --drill sentry-path
python scripts/run_drill.py --drill unknown-path-scan
python scripts/run_drill.py --drill readiness-failure     # prints the manual steps

# Machine-readable, for CI.
python scripts/run_drill.py --drill unhandled-exception --json
```

Each drill exits non-zero if it fails, so it can gate a pipeline. `run_drill.py`
produces a markdown section identical in shape to the ones below.

---

## Results

**Environment:** local development, `manage.py runserver` (single process),
SQLite, `DEBUG=True`, Sentry disabled (no DSN).
**Recorded:** 2026-09-20.

A single-process, single-worker environment is the *easiest* case. Latency to
detection will be higher behind a real scrape interval (15 s in this stack), and
that difference is the point of recording the environment alongside the number.

### Drill: unhandled exception is captured, counted and correlated

| Field | Value |
|---|---|
| Injected | `GET /boom/` (raises `RuntimeError`) |
| Detected by | `app_errors_total` in `/metrics` |
| Time to detect | **0.01 s** |
| Log correlation | **request id matched** |
| Outcome | pass |

Evidence:

- `app_errors_total{endpoint="/boom/",error_type="RuntimeError"} before = 3`
- `GET /boom/ -> 500 in 26 ms`
- `app_errors_total{endpoint="/boom/",error_type="RuntimeError"} after = 4 (delta 1)`
- log line: `message=unhandled_exception endpoint=/boom/ error_type=RuntimeError`

**What this actually proves.** Three things, and each was a design decision that
could have been wrong:

1. **The exception is observed at all.** Django wraps every middleware in
   `convert_exception_to_response`, so an exception raised in a view never crosses
   a middleware boundary — a naive `try/except` around `self.get_response(request)`
   would never fire, and the requests that crashed would be the only ones missing
   from the error metrics. Detection goes through the `process_exception` hook
   instead.
2. **Counted exactly once.** The delta is 1, not 2. Two observers see the same
   failure (the exception hook and the 500 response that Django builds afterwards);
   a flag on the request stops them both counting it.
3. **Correlated end to end.** The `X-Request-ID` sent by the client appears
   verbatim in the server's structured log. That is the mechanism by which a user's
   bug report becomes a specific log line.

### Drill: a second failure mode follows the same detection path

| Field | Value |
|---|---|
| Injected | `GET /sentry-debug/` (raises `ZeroDivisionError`) |
| Detected by | `app_errors_total` in `/metrics` |
| Time to detect | **0.01 s** |
| Log correlation | request id matched |
| Outcome | pass |

Evidence:

- `GET /sentry-debug/ -> 500`
- `app_errors_total{error_type="ZeroDivisionError"} = 1 (delta 1)`
- log line: `message=unhandled_exception endpoint=/sentry-debug/ error_type=ZeroDivisionError`

**What this proves.** The error metric is labelled by *exception type*, so a
`ZeroDivisionError` is distinguishable from a `RuntimeError` on a dashboard without
reading any logs. Two different failure modes produce two different series.

### Drill: unknown paths collapse into one bounded metric series

| Field | Value |
|---|---|
| Injected | 200 requests for random non-existent paths |
| Detected by | `app_http_requests_total{endpoint="unmatched"}` |
| Time to detect | 1.49 s (200 requests) |
| Log correlation | n/a |
| Outcome | pass |

Evidence:

- `app_http_requests_total{endpoint="unmatched"} delta = 200 for 200 distinct URLs`
- distinct `unmatched` series = **1**

**What this proves.** The cardinality guard holds under a scanner. 200 distinct
URLs produced one time series, not 200. This is the failure mode that takes a
Prometheus server down slowly: label values that grow with input. It cannot be
tested in production (the damage is already done by the time it is visible), which
is why it is a drill.

### Drill: readiness probe takes the instance out of rotation (manual)

| Field | Value |
|---|---|
| Injected | stop PostgreSQL, or point `DATABASE_URL` at a dead host |
| Detected by | `/readyz/` returns 503; `app_readiness_failures_total` rises |
| Time to detect | one probe interval |
| Outcome | procedure documented, not automated |

This one cannot be driven over HTTP: it requires a dependency to actually fail.

**Procedure.**

```bash
# 1. Baseline.
curl -sS -o /dev/null -w 'readyz %{http_code}\n' http://localhost:8000/readyz/

# 2. Break the dependency.
docker compose stop db

# 3. Poll readiness. Expect 503 with the failing dependency named.
for i in $(seq 1 10); do
  curl -sS http://localhost:8000/readyz/ | python -m json.tool
  sleep 2
done

# 4. Confirm liveness is STILL 200. This is the whole point.
curl -sS -o /dev/null -w 'healthz %{http_code}\n' http://localhost:8000/healthz/

# 5. Confirm the counter rose.
curl -sS http://localhost:8000/metrics | grep '^app_readiness_failures_total'

# 6. Restore.
docker compose start db
curl -sS -o /dev/null -w 'readyz %{http_code}\n' http://localhost:8000/readyz/
```

**Expected result.** `/readyz/` returns 503 with
`checks.database.ok == false`; `/healthz/` returns **200 throughout**;
`app_readiness_failures_total{dependency="database"}` increases; `up` stays 1.

**Why liveness must stay 200.** A liveness probe failing means "restart this
process". If `/healthz/` also checked the database, a database outage would make
the orchestrator restart every healthy worker in a loop — turning someone else's
outage into a self-inflicted denial of service. `ops/tests/test_probes.py` has a
test that fails if a database call is ever added to the liveness view.

**Observed on 2026-09-20:** the readiness failure path was verified by unit test
rather than by stopping a live database; `ops/tests/test_probes.py::TestReadiness`
asserts the 503, the named dependency, the counter increment and the latency
reporting, with the database cursor mocked to raise. The end-to-end version above
has not been executed against a real stopped database. **That is a gap, and it is
recorded here rather than implied away.**

---

## What is *not* covered

| Gap | Why | Impact |
|---|---|---|
| Alert firing and delivery | No Alertmanager in this stack | A critical alert appears in the Prometheus UI and is not delivered anywhere. See [KNOWN_ISSUES #8](KNOWN_ISSUES.md) |
| Sentry event delivery | No DSN configured in this environment | The integration is unit-tested; the network hop is not. See [KNOWN_ISSUES #12](KNOWN_ISSUES.md) |
| Detection latency behind a real scrape | Drills ran against a local instance, polling `/metrics` directly | In production, detection is bounded by the 15 s scrape interval plus the alert's `for:` window. The `0.01 s` figures above are the *floor*, not the expected value |
| Multi-worker behaviour | Single worker by design | See [KNOWN_ISSUES #1](KNOWN_ISSUES.md) |
| Disk exhaustion | No `node_exporter` | See [KNOWN_ISSUES #9](KNOWN_ISSUES.md) |

Stating the gaps is the point of the log. A drill log that only lists successes
tells the reader nothing about where to look next.

## Cadence

Run the automated drills before every release that touches the observability path
(`ops/`, `settings.py`, alert rules, dashboards). The whole set takes under five
seconds and the alternative is discovering that a rename broke detection during an
incident.

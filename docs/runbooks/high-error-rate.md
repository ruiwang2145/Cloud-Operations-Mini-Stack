# Runbook: elevated error rate

**Alerts:** `HighErrorRate` (critical), `AvailabilityBudgetBurnFast` (critical),
`AvailabilityBudgetBurnSlow` (warning), `ErrorBudgetExhausted` (warning)

## Symptoms

| Alert | What it means |
|---|---|
| `HighErrorRate` | More than 5% of requests returned 5xx over 5 minutes |
| `AvailabilityBudgetBurnFast` | Burning the 30-day error budget at 14.4× the sustainable rate over 1 hour |
| `AvailabilityBudgetBurnSlow` | Burning at 6× over 6 hours |
| `ErrorBudgetExhausted` | Measured availability is below the 99.5% objective |

They escalate in that order. `HighErrorRate` is "something is broken now";
the burn alerts are "this rate is not survivable"; `ErrorBudgetExhausted` is
"the policy in [SLO.md](../SLO.md) now applies".

## Impact

Depends entirely on *which* endpoint is failing. A 5xx on `/api/tasks/stats/`
affects a dashboard tile; a 5xx on `/api/tasks/` affects everything. **Find out
which before deciding how hard to push.**

## First five minutes

```bash
# 1. Is it still happening? A spike that already ended needs a different response
#    (find the cause, do not roll back blindly).
curl -sS http://localhost:8000/api/slo/ | python -m json.tool

# 2. WHICH endpoint? This is the single most useful question.
#    http://localhost:9090/graph
#    sum by (endpoint) (rate(app_http_requests_total{status_class="5xx"}[5m]))
curl -sS http://localhost:9090/api/v1/query --data-urlencode \
  'query=sum by (endpoint) (rate(app_http_requests_total{status_class="5xx"}[5m]))' \
  | python -m json.tool

# 3. WHAT kind of error? The exception type names the cause.
curl -sS http://localhost:9090/api/v1/query --data-urlencode \
  'query=sum by (error_type) (rate(app_errors_total[5m]))' | python -m json.tool

# 4. The actual stack traces, most recent first.
python - <<'PY'
import json, pathlib
for line in reversed(pathlib.Path("app.log").read_text(encoding="utf-8").splitlines()[-2000:]):
    event = json.loads(line)
    if event.get("level") == "ERROR":
        exc = event.get("exception", {})
        print(f"{event['ts']}  {event.get('endpoint')}  {exc.get('type')}: {exc.get('message')}")
        print(f"   request_id={event.get('request_id')}")
        for frame in exc.get("stacktrace", [])[-4:]:
            print(f"   {frame}")
        print()
PY
```

At this point you should know: which endpoint, which exception, since when.

## Diagnose

Work through these in order. Each one either identifies the cause or eliminates a
large class of them.

### 1. Did a deploy happen?

```bash
# Which version is actually serving traffic?
curl -sS http://localhost:8000/version/ | python -m json.tool
curl -sS http://localhost:8000/metrics | grep app_build_info

# When did the error rate start? Compare with the deploy time.
# http://localhost:9090/graph
#   sum(rate(app_http_requests_total{status_class="5xx"}[5m]))
```

An error rate that steps up at a deploy boundary is a code change. Stop here and
go to [deploy-and-rollback.md](deploy-and-rollback.md) — do not debug forward on a
live service when a rollback is available and takes two minutes.

### 2. Is it one exception type or many?

```bash
curl -sS http://localhost:8000/metrics | grep '^app_errors_total'
```

- **One type, one endpoint** → a specific code path. Read the stack trace.
- **One type, every endpoint** → a shared dependency. Almost always the database:
  [database-unreachable.md](database-unreachable.md).
- **Many types at once** → something systemic. Check the database, then memory,
  then the disk ([disk-and-log-growth.md](disk-and-log-growth.md)).

### 3. Is the 4xx rate also up?

```bash
curl -sS http://localhost:9090/api/v1/query --data-urlencode \
  'query=sum by (status_class) (rate(app_http_requests_total[5m]))' | python -m json.tool
```

4xx rising alongside 5xx usually means a client changed: an expired token, a new
integration sending malformed payloads, or a scanner. 4xx does **not** count
against the availability SLI — see [SLO.md](../SLO.md) for why — so it will not
explain a burn alert, but it is often the actual cause of the 5xx that follows.

### 4. Is it load?

```bash
# Request rate and error rate on the same graph, side by side.
#   sum(rate(app_http_requests_total{endpoint!~"/healthz/|/metrics|/readyz/|/version/"}[5m]))
#   sum(rate(app_http_requests_total{status_class="5xx"}[5m]))
```

If errors rise with traffic, look for a connection pool limit, a timeout that is
too short under load, or a single slow query that is queueing. See
[high-latency.md](high-latency.md).

## Mitigate

In order of preference. Each is a real mitigation, not a workaround:

1. **Roll back** if a deploy is implicated. Fastest, safest, and reversible.
   → [deploy-and-rollback.md](deploy-and-rollback.md)
2. **Take the bad instance out of rotation** if only some instances are failing.
   `readiness` returning 503 does this automatically — fix the dependency rather
   than the probe.
3. **Fail the endpoint closed** if it is non-essential. Returning a cached or
   empty response for a dashboard tile is better than returning 500s that burn the
   whole budget.
4. **Fix the dependency** if the cause is the database. → [database-unreachable.md](database-unreachable.md)

Do **not** silence the alert to make the dashboard green. The alert is the
symptom; muting it removes your ability to see the next one.

## Verify

```bash
# The rate must be falling, not merely below the threshold.
curl -sS http://localhost:9090/api/v1/query --data-urlencode \
  'query=sum(rate(app_http_requests_total{status_class="5xx"}[5m]))' | python -m json.tool

# And the budget must have stopped draining.
curl -sS http://localhost:8000/api/slo/ | python -m json.tool
```

- [ ] 5xx rate back to its normal baseline (not just under 5%)
- [ ] `app_slo_error_budget_remaining_ratio` is no longer falling
- [ ] `python scripts/smoke_test.py` passes
- [ ] The specific failing endpoint returns 200 under a real request

`HighErrorRate` clears after 5 quiet minutes. `ErrorBudgetExhausted` will **not**
clear for the rest of the window, because the budget really is spent — that is
what the policy in [SLO.md](../SLO.md) is for.

## Follow-up

- Was there a test that would have caught this? If not, why not — and is it worth
  writing, or is it a genuinely novel failure?
- Did an alert fire, or did you find out from a user? If the latter, the alerting
  has a gap, and the gap is more important than the bug.
- Add the exception type to [KNOWN_ISSUES.md](../KNOWN_ISSUES.md) if it is
  something that will recur.

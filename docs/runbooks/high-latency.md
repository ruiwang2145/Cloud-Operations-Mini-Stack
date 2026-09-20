# Runbook: high latency

**Alert:** `LatencyObjectiveBreach` (warning)

## Symptoms

The share of requests answered within 300 ms has fallen below 99% for 10 minutes:

```promql
sum(rate(app_http_request_duration_seconds_bucket{le="0.3"}[10m]))
/
sum(rate(app_http_request_duration_seconds_count[10m])) < 0.99
```

Users may not have complained yet. This alert exists precisely because "slow" is
the failure mode that nobody reports and everybody resents.

## Impact

Latency is not a binary failure. What matters:

- **Which percentile moved.** p50 rising means every request is slower — a real
  regression. Only p99 rising means a tail problem — usually a lock, a garbage
  collection pause, or one slow dependency hit by a subset of requests.
- **Which endpoint moved.** One endpoint is a bug; all endpoints is infrastructure.

## First five minutes

```bash
# 1. Percentiles, all endpoints. Is it the median or the tail?
#    http://localhost:9090/graph
#    histogram_quantile(0.50, sum by (le) (rate(app_http_request_duration_seconds_bucket[5m])))
#    histogram_quantile(0.95, sum by (le) (rate(app_http_request_duration_seconds_bucket[5m])))
#    histogram_quantile(0.99, sum by (le) (rate(app_http_request_duration_seconds_bucket[5m])))
curl -sS http://localhost:9090/api/v1/query --data-urlencode \
  'query=histogram_quantile(0.95, sum by (le) (rate(app_http_request_duration_seconds_bucket[5m])))' \
  | python -m json.tool

# 2. Which endpoint? p95 per endpoint.
curl -sS http://localhost:9090/api/v1/query --data-urlencode \
  'query=histogram_quantile(0.95, sum by (le, endpoint) (rate(app_http_request_duration_seconds_bucket[5m])))' \
  | python -m json.tool

# 3. Is traffic also up? Slow because busy is a different problem from slow because broken.
curl -sS http://localhost:9090/api/v1/query --data-urlencode \
  'query=sum(rate(app_http_requests_total[5m]))' | python -m json.tool
```

## Diagnose

### 1. Is the median slow, or only the tail?

| Shape | Usual cause |
|---|---|
| p50 and p99 both up | Every request pays the cost: a slow query on the hot path, a synchronous call to a slow dependency, CPU starvation |
| p50 flat, p99 up | A subset of requests is slow: lock contention, cold cache, one shard, a slow client |

The p50/p99 split is the most informative single signal here, and it takes ten
seconds to look at.

### 2. Did the request rate rise at the same time?

Slow *and* busier means capacity. Slow at a **constant** rate means something got
slower — a regression, a degraded dependency, or a missing index.

```bash
# Watch the two together:
#   sum(rate(app_http_requests_total{endpoint!~"/healthz/|/metrics|/readyz/|/version/"}[5m]))
#   sum(rate(app_http_request_duration_seconds_count{endpoint!~"/healthz/|/metrics|/readyz/|/version/"}[5m]))
```

### 3. Is the database the bottleneck?

```bash
# Database time, if you enabled the django_prometheus DB backend
# (see docs/architecture.md -- it is not on by default).
curl -sS http://localhost:8000/metrics | grep '^django_db_'
```

If it is not enabled, use the database's own view instead:

```bash
docker compose exec db psql -U cloudops -d cloudops -c "
  SELECT pid, now() - query_start AS duration, state, left(query, 80) AS query
  FROM pg_stat_activity
  WHERE state <> 'idle'
  ORDER BY duration DESC
  LIMIT 10;"
```

A long-running query blocking others shows up here immediately. This is the
single most common cause of a latency spike in a Django service.

### 4. Did a deploy happen?

Compare the alert start time against the last deploy. A step change at a deploy
boundary is a code change — check for a new query in a loop, a missing index, or a
synchronous call added to the hot path.

### 5. Is the process starved?

```bash
docker stats --no-stream
```

Look at CPU and memory. Memory climbing towards the limit produces a rising p99
before it produces a crash, because the process spends its time in the allocator.

## Mitigate

1. **If a deploy is implicated, roll back.** → [deploy-and-rollback.md](deploy-and-rollback.md)
2. **If a single query is responsible**, kill it:
   ```bash
   docker compose exec db psql -U cloudops -d cloudops \
     -c "SELECT pg_cancel_backend(<pid>);"
   ```
   `pg_cancel_backend` is polite and lets the transaction roll back cleanly.
   `pg_terminate_backend` is not, and is the escalation.
3. **If it is capacity**, add workers — but read
   [the multi-worker caveat](../KNOWN_ISSUES.md#1-metrics-are-per-worker-under-gunicorn)
   first, because more workers change what the metrics mean.
4. **Shed load** if the cause is a runaway client. The API throttles anonymous
   requests at `API_ANON_THROTTLE` (default `120/min`); lower it.

## Verify

```bash
# Compliance must be back above 0.99, and staying there.
curl -sS http://localhost:9090/api/v1/query --data-urlencode \
  'query=sum(rate(app_http_request_duration_seconds_bucket{le="0.3"}[5m])) / sum(rate(app_http_request_duration_seconds_count[5m]))' \
  | python -m json.tool

python scripts/smoke_test.py
```

- [ ] Compliance above 0.99 for at least 10 minutes (the alert's `for` window)
- [ ] p95 back to its pre-incident value
- [ ] The smoke test passes — and its per-check timings are back to single-digit
      milliseconds, which is the cheapest end-to-end latency check available

## Follow-up

- Was the 300 ms objective realistic for this endpoint, or is the objective wrong
  rather than the code? Both are legitimate outcomes, and the second one is a
  change to `docs/SLO.md` plus `settings.py` — not a change to the alert.
- If the cause was a query, was it missing an index, or was it missing a
  `select_related`? `tasks/tests/test_api.py::TestQueryCount` exists to catch the
  second kind; if it did not, the test needs widening.
- Add the trigger to [KNOWN_ISSUES.md](../KNOWN_ISSUES.md) if it will recur.

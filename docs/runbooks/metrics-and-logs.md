# Runbook: metrics and logs

**Alert:** `SloReporterDegraded` (warning)

## Symptoms

- `SloReporterDegraded`: the SLO reporter could not reach Prometheus and fell
  back to the in-process registry. `/api/slo/` still answers, but reports
  `"window_source": "process_lifetime"` instead of the configured window.
- Or, more generally: you need to know what the service is doing and the
  dashboards are not telling you.

## The four places to look

| Question | Where | Latency |
|---|---|---|
| Is it up right now? | `/healthz/`, `/readyz/` | instant |
| What has it been doing? | Prometheus, Grafana | one scrape interval (15 s) |
| What exactly happened? | structured logs | instant (local) |
| What did the exception look like? | Sentry | seconds |

Work them in that order. The fastest answer is usually the right one to start
with.

## 1. Raw metrics

```bash
# Everything the service exports.
curl -sS http://localhost:8000/metrics

# Just the metric families, to see what is available.
curl -sS http://localhost:8000/metrics | grep '^# TYPE' | awk '{print $3, $4}'

# Just our own application metrics, without the framework noise.
curl -sS http://localhost:8000/metrics | grep '^app_'
```

Remember the deliberate absence of a trailing slash: `/metrics` works,
`/metrics/` is a 404. A scrape config with the slash looks correct and collects
nothing.

## 2. Prometheus

```bash
# Is the target healthy? This is the first question when a dashboard is empty.
curl -sS http://localhost:9090/api/v1/targets | python -m json.tool | head -40

# A one-off query, without the UI.
curl -sS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=sum(rate(app_http_requests_total[5m]))' | python -m json.tool

# Are the alert rules loaded and evaluating?
curl -sS http://localhost:9090/api/v1/rules | python -m json.tool | head -60

# Which alerts are firing?
curl -sS http://localhost:9090/api/v1/alerts | python -m json.tool
```

Reload the config after editing it — no restart needed, because
`--web.enable-lifecycle` is set:

```bash
curl -sS -X POST http://localhost:9090/-/reload
```

### "The graph is empty"

In order of likelihood:

1. **The metric name changed.** Compare against `curl -sS .../metrics | grep '^# TYPE'`.
   `ops/tests/test_dashboards.py` fails the build when a dashboard references a
   metric that does not exist, so this should be caught before it ships.
2. **The label selector matches nothing.** An `endpoint="..."` that no longer
   exists returns an empty vector, which renders identically to zero.
3. **The time range is wrong.** A dashboard defaulting to `now-6h` shows nothing
   for data from yesterday.
4. **The target is down.** Check `/api/v1/targets` before anything else.

## 3. Structured logs

Every line is one JSON object. That means the log is queryable, not just readable.

```bash
# Follow the service.
docker compose logs -f web

# Everything at ERROR level, pretty-printed.
python - <<'PY'
import json, pathlib
for line in pathlib.Path("app.log").read_text(encoding="utf-8").splitlines():
    event = json.loads(line)
    if event["level"] == "ERROR":
        print(json.dumps(event, indent=2))
PY
```

### The one query that answers most questions

```bash
# Follow a single request end to end.
python - <<'PY'
import json, pathlib, sys
request_id = sys.argv[1] if len(sys.argv) > 1 else "<paste the X-Request-ID here>"
for line in pathlib.Path("app.log").read_text(encoding="utf-8").splitlines():
    if request_id in line:
        print(json.dumps(json.loads(line), indent=2))
PY
```

`X-Request-ID` is echoed on every response, so a user reporting a failure can give
you the one value that ties their report to the exact log line, the exact metric
increment and the exact Sentry event. This is the reason the correlation id exists.

### Event names, not sentences

`message` is an event name (`http_request`, `unhandled_exception`,
`readiness_check_failed`), which makes it countable:

```bash
# How many requests failed, by endpoint, in this log?
python - <<'PY'
import collections, json, pathlib
counter = collections.Counter()
for line in pathlib.Path("app.log").read_text(encoding="utf-8").splitlines():
    event = json.loads(line)
    if event.get("message") == "http_request" and event["http"]["status"] >= 500:
        counter[event["http"]["endpoint"]] += 1
for endpoint, count in counter.most_common():
    print(f"{count:5}  {endpoint}")
PY
```

## 4. Sentry

Only active when `SENTRY_DSN` is set. Verify:

```bash
curl -sS http://localhost:8000/version/ | python -m json.tool
docker compose logs web | grep -i sentry
```

Probe failures (`/healthz/`, `/readyz/`, `/metrics`) are filtered out by
`ops/sentry_filters.py::drop_probe_events`, **except** 5xx responses from those
paths — a failing health check is a real outage and must be reported.

If Sentry is quiet when it should not be, check in this order: is `SENTRY_DSN`
set? does the DSN have the right environment? is the event being filtered by
`before_send`? is `SENTRY_TRACES_SAMPLE_RATE` so low that this request was not
sampled?

## 5. The SLO reporter is degraded

`SloReporterDegraded` means `PROMETHEUS_URL` is set but the query failed.

```bash
# Is Prometheus reachable from the application container? Not from your laptop --
# from inside the container, which is where the query originates.
docker compose exec web python - <<'PY'
import os, urllib.request
url = os.environ.get("PROMETHEUS_URL", "")
print("PROMETHEUS_URL =", url or "(not set)")
if url:
    try:
        with urllib.request.urlopen(f"{url}/api/v1/query?query=up", timeout=5) as response:
            print("reachable, status", response.status)
    except Exception as exc:
        print("UNREACHABLE:", exc)
PY
```

The most common cause is the URL pointing at `localhost:9090` from inside a
container, where `localhost` is the application container itself. It has to be the
compose service name: `http://prometheus:9090`.

The report degrades rather than failing — that is deliberate. A monitoring
endpoint that returns 500 because monitoring is down is useless precisely when it
is needed. But **the fallback is not equivalent**: it reports the lifetime of the
process, not the configured window, and `window_source` in the response says so.
Do not make decisions about a 30-day budget from a five-minute process.

## Verify

```bash
curl -sS http://localhost:8000/api/slo/ | python -m json.tool
# Expect: "source": "prometheus", "window_source": "30d"
```

- [ ] `source` is `prometheus`, not `local_registry`
- [ ] `window_source` is the configured window, not `process_lifetime`
- [ ] `increase(app_slo_evaluation_fallbacks_total[15m])` has stopped rising
- [ ] `/api/v1/targets` in Prometheus shows the target as `up`

## Related

- [high-latency.md](high-latency.md) — the per-endpoint breakdown
- [SLO.md](../SLO.md) — what the numbers mean
- [architecture.md](../architecture.md) — why the fallback exists at all

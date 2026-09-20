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

# The readiness drill needs the service up and its database down, which
# `runserver` cannot do. Start the service with scripts/serve_for_drills.py
# instead, then run the drill against it. See the drill below.
python scripts/run_drill.py --drill readiness-failure

# Machine-readable, for CI.
python scripts/run_drill.py --drill unhandled-exception --json
```

Each drill exits non-zero if it fails, so it can gate a pipeline. `run_drill.py`
produces a markdown section identical in shape to the ones below.

To verify the *alerting* path rather than just the metrics, Prometheus has to be
running. [native-monitoring.md](native-monitoring.md) shows how to do that without
Docker.

---

## Results

Three runs are recorded below.

**Run A — drill scripts, direct metric polling.**
`manage.py runserver` (single process), SQLite, `DEBUG=True`, Sentry disabled (no
DSN). Recorded 2026-09-20. A single-process environment is the *easiest* case:
detection latency is measured by polling `/metrics` directly, with no scrape
interval in the path. These numbers are a **floor**, not an expectation.

**Run B — full monitoring stack, native.**
Prometheus 3.14.0 and Grafana 13.2.2 running natively (no Docker, no container
runtime — see [native-monitoring.md](native-monitoring.md)), scraping the
application every 15 seconds. Recorded 2026-09-20. This is the run that answers
the questions Run A cannot: whether the rules evaluate against real data, whether
they actually fire, and whether the dashboards render. Its results are in
[Run B](#run-b--the-alerting-path-under-a-real-scrape-interval).

**Run C — the container path.**
The real thing: `docker compose up -d --build`, four containers, the application
served by gunicorn from the built image. Recorded 2026-09-20. This is the run that
answers the remaining question — whether the `Dockerfile`, the entrypoint, the
healthcheck and compose service discovery actually work — and it is the one that
**failed first and had to be fixed**. See
[Run C](#run-c--the-container-path).

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

### Drill: readiness probe takes the instance out of rotation

| Field | Value |
|---|---|
| Injected | `DATABASE_URL` pointed at a dead PostgreSQL endpoint |
| Detected by | `/readyz/` returns 503; `app_readiness_failures_total` rises |
| Time to detect | one probe interval |
| Outcome | **pass — executed end to end** |

This one cannot be driven over HTTP: it requires a dependency to actually fail.

In the compose stack the equivalent is `docker compose stop db`, then
`docker compose start db` to recover — the web container keeps running, which is
the whole point.

**Procedure.**

```bash
# 1. Start the service with the database unreachable.
#    NOTE: `manage.py runserver` cannot be used here. It calls check_migrations()
#    during startup, which needs a connection, so the process exits before it can
#    ever answer /readyz/. gunicorn does no such check -- which is exactly why a
#    production container can be up-but-not-ready. This launcher reproduces
#    gunicorn's behaviour with the standard library alone.
DATABASE_URL=postgresql://bad:bad@127.0.0.1:59999/bad \
  .venv/bin/python scripts/serve_for_drills.py

# 2. Readiness must fail, naming the dependency and reporting how long the check took.
curl -sS http://127.0.0.1:8000/readyz/

# 3. Liveness must STILL be 200. This is the whole point.
curl -o /dev/null -w 'healthz %{http_code}\n' http://127.0.0.1:8000/healthz/

# 4. The counter must rise, once per failed probe.
curl -sS http://127.0.0.1:8000/metrics | grep '^app_readiness_failures_total'

# 5. With Prometheus running, the alert must fire.
curl -sS http://127.0.0.1:9090/api/v1/alerts
```

**Observed on 2026-09-20 (Run B, executed end to end):**

```
readyz 1 -> 503  (5.019440s)
readyz 2 -> 503  (5.020691s)
...
readyz 8 -> 503  (5.016867s)

app_readiness_failures_total{dependency="database"} 9.0
healthz -> 200
```

```json
{
  "status": "not_ready",
  "checks": {
    "database": {
      "ok": false,
      "error": "OperationalError: connection timeout expired",
      "latency_ms": 5009.78
    }
  },
  "request_id": "e6f8cfbedb9e43dc"
}
```

and in Prometheus:

```
ReadinessCheckFailing   firing   severity=warning   value=15.85
    summary: Readiness probe failing for dependency database
    runbook: docs/runbooks/database-unreachable.md
```

Four things this proves, each of which was a design decision that could have been
wrong:

1. **Liveness survived a total database outage.** 200 throughout, while readiness
   failed. If liveness also touched the database, the orchestrator would have
   restarted every healthy worker in a loop — turning someone else's outage into a
   self-inflicted denial of service.
2. **Readiness failed closed and named the dependency.** 503, not 200-with-a-warning,
   and the response says which dependency and why.
3. **The failure took exactly `DB_CONNECT_TIMEOUT` (5 s).** The 5009.78 ms latency
   is the timeout doing its job. Without it the probe would hang until the
   orchestrator's own timeout fired, and the log would say "probe timed out"
   instead of "connection timeout expired" — losing the diagnosis. This is also
   what the diagnostic table in
   [database-unreachable.md](runbooks/database-unreachable.md) is keyed on: ~0 ms
   is a refusal, ~5000 ms is a timeout, and they have different causes.
4. **The alert fired from real data.** `ReadinessCheckFailing` is driven by
   `increase(app_readiness_failures_total[5m]) > 0`, so it fired without anyone
   touching the rules.

---

## Run B — the alerting path under a real scrape interval

Everything above was measured by polling `/metrics` directly. That proves the
application exports the right numbers; it says nothing about whether the rules
evaluate, whether they fire, or whether the dashboards render. This run covers
that, with Prometheus and Grafana running natively and scraping every 15 seconds.

### Every rule expression is valid against real data

`/api/v1/rules` reports `health=ok` for all nine, which means every PromQL
expression parsed *and* executed. A typo in a metric name or a label produces
`health=err` here and a silent no-op in production.

`promtool` agrees, before Prometheus is even started:

```
$ promtool check rules monitoring/prometheus/alert_rules.yml
  SUCCESS: 9 rules found

$ promtool check config monitoring/prometheus/prometheus.local.yml
  SUCCESS: 1 rule files found
  SUCCESS: ... is valid prometheus config file syntax
```

### The alerts fire when they should, and only then

Baseline, with ~1% injected errors — nothing fires, correctly:

| Alert | State |
|---|---|
| `AvailabilityBudgetBurnFast` | inactive (burn rate 2.5, threshold 14.4) |
| `HighErrorRate` | inactive (1.3% errors, threshold 5%) |
| `ErrorBudgetExhausted` | pending (budget 0.0, `for: 10m` not yet elapsed) |
| `LatencyObjectiveBreach` | inactive (compliance 100%, threshold 99%) |
| `ServiceDown` | inactive (target is up) |

Then 8 minutes at ~15% injected errors, 3 793 requests:

| Alert | State | Value | Threshold |
|---|---|---|---|
| `AvailabilityBudgetBurnFast` | **firing** (critical) | 23.3 | > 14.4 |
| `HighErrorRate` | **firing** (critical) | 15.5% | > 5% |
| `AvailabilityBudgetBurnSlow` | pending (warning) | 23.3 | > 6, `for: 30m` |
| `ErrorBudgetExhausted` | pending (warning) | 0 | ≤ 0, `for: 10m` |

**Two things worth reading off this table.** The burn-rate and the absolute error
rate agree with each other and with the arithmetic: 15.5% errors against a 0.5%
budget is a burn rate of 31, and the 1-hour window reports 23.3 because it still
contains earlier healthy traffic. And the `for:` windows behave as designed — the
2-minute rule fired first, the 5-minute rule second, and the 30-minute and
10-minute rules were still pending when the run ended. **An alert that fires
immediately is an alert that fires on every blip.**

### Every dashboard panel renders

All 27 panel queries from both dashboards were executed through Grafana's own
datasource proxy, so a panel that would render empty is caught as an empty result
rather than discovered by squinting at a screenshot:

```
27 panel queries executed   |   27 returned data   |   0 empty   |   0 errored
```

Before the readiness drill, 26 of 27 returned data — the exception was
`Readiness probe failures by dependency`, which was empty because **no readiness
check had ever failed**. That is the correct state, and it is why the panel is
listed in the "not covered" table only until the drill is run. After the drill it
reported `1 series, 24 points`.

### An unplanned finding: the throttle works

The 15% burst was driven at 8 req/s, well above the anonymous throttle of
`120/min`. The load generator's own counters recorded the result:

```
3793 requests (200:1688, 403:431, 404:67, 429:1062, 500:545)
```

1 062 responses were `429 Too Many Requests`. That is `AnonRateThrottle` doing its
job, and it is the reason the injected 15% error rate surfaced as 14.4% of total
traffic: the throttle was shedding load before it reached the fault endpoint. Not
a designed test, but a real one.

---

## Run C — the container path

Runs A and B used native binaries and a SQLite database. Neither of them built the
image, started the entrypoint, ran a migration through compose, or let a
healthcheck decide whether the service was ready. This run does all of that, with
`docker compose up -d --build`.

**Environment.** Windows 10 Home (Build 19044) — Docker Desktop is not supported
here (it requires Build 19045, and Home has no Hyper-V), so this is WSL2 with
Docker Engine 29.8.1 on an imported Ubuntu 24.04 rootfs. Four containers:
`db` (`postgres:17-alpine`), `web` (the built image), `prometheus` (`v3.14.0`),
`grafana` (`13.2.2`).

### It did not work the first time

Worth leading with, because it is the most useful thing in this document.

The first `docker compose up` started all four containers. Port 9090 answered 200
and so did 3000 — but 8000 never did:

```
[entrypoint] collecting static files
Traceback (most recent call last):
  ...
PermissionError: [Errno 13] Permission denied: '/app/staticfiles'
[entrypoint] waiting for the database to accept connections     <- second start
```

That last line is the tell. `collectstatic` failed, and then the entrypoint
started *again* — so the container was not dead, it was in a crash loop, and
`restart: unless-stopped` was faithfully restarting it every couple of seconds.

**Root cause: three files, each individually correct.**

| File | What it does right | Why the combination fails |
|---|---|---|
| `Dockerfile` | `WORKDIR /app`, then `COPY --chown=appuser:appuser . .` | `WORKDIR` creates `/app` as **root**, and `COPY --chown` re-owns the files it copies — *not the directory they land in*. So `appuser` cannot create anything inside `/app` |
| `.dockerignore` | Excludes `staticfiles/`, keeping the build context small | The directory therefore does not exist in the image |
| `scripts/docker-entrypoint.sh` | `set -eu`, then `collectstatic` as step 4 | Correct: a failed setup step must stop the start. Which it does |
| `docker-compose.yml` | `restart: unless-stopped` | Turns "exits after two seconds" into a loop that never converges |

Nothing here is a typo. Every file is defensible on its own, and the failure only
exists in the seam between them.

**The fix**, before the `USER` switch:

```dockerfile
RUN mkdir -p /app/staticfiles \
    && chown appuser:appuser /app/staticfiles
```

One directory is handed over; the rest of the source tree stays read-only to the
process.

**Why not collect static files at build time.** That is the more orthodox answer —
an immutable image, a faster start, and a process that never needs write access.
It is not available here: `settings.py` raises `RuntimeError` when `SECRET_KEY` is
absent and `DEBUG` is off, and the build has no `SECRET_KEY`. Passing one as a
build argument would bake a secret into an image layer. Runtime collection is
therefore the only viable design for this project, which makes the directory's
writability load-bearing rather than incidental. The reasoning lives in a comment
in the `Dockerfile`.

**A regression guard.** `ops/tests/test_container_contract.py` (23 tests) pins the
cross-file invariants that a container runtime enforces but a unit test normally
cannot see. The important one ties the `Dockerfile` to `settings.py`: it derives
the container path of `STATIC_ROOT`, asserts that both the `mkdir` and the `chown`
are present, and asserts that the `chown` precedes the `USER` line — because the
ordering *is* the fix. Deleting those two lines fails three tests, by name.

### After the fix

```
#18 [runtime 8/8] RUN mkdir -p /app/staticfiles     && chown appuser:appuser /app/staticfiles
...
[entrypoint] database reachable after 1 attempt(s)
[entrypoint] applying database migrations
  No migrations to apply.
[entrypoint] collecting static files
154 static files copied to '/app/staticfiles', 145 post-processed.
[entrypoint] starting: gunicorn my_cloudapp.wsgi:application --bind 0.0.0.0:8000 --workers 1 ...
[INFO] Listening at: http://0.0.0.0:8000 (1)
```

`web` reported `healthy` **10 seconds** after the recreate — the healthcheck's
start period doing its job rather than declaring the service up while the
entrypoint was still running.

### What Run C verifies

| Check | Result |
|---|---|
| `docker compose ps` | `db` healthy; `web`, `prometheus`, `grafana` up, ports 8000 / 9090 / 3000 published |
| Every endpoint, probed **inside** the container | `/healthz/` 200 · `/readyz/` 200 · `/version/` 200 · `/api/tasks/` 200 · `/metrics` 200 · `/api/slo/` 200 |
| Prometheus targets | `web:8000` **up**, `localhost:9090` **up** |
| Prometheus rule health | 9 of 9 rules `health=ok` |
| Grafana | `/api/health` → `database: ok`, version 13.2.2 |
| `scripts/smoke_test.py` from the host | **11 passed, 0 failed** |
| `run_drill.py --drill unhandled-exception` | **pass**, detected in 0.02 s, counter delta exactly 1 |
| `run_drill.py --drill unknown-path-scan` | **pass**, 200 distinct URLs → 1 `unmatched` series |

The probes are run *inside* the container on purpose: if something is broken, the
first question is whether it is the application or the path to it, and probing
from both sides answers that immediately.

**One transient worth recording.** Immediately after the recreate, Prometheus
reported one target `down`. That is correct and expected — the scrape landed in
the window where `web` was restarting — and the next cycle showed both `up`. A
monitoring stack that reports a restart as an outage for one scrape interval is
behaving properly; one that hides it would not be.

### Log correlation in the container

The drill reports `Log correlation: not checked` here, and that is the honest
answer: the container logs to **stdout**, which is the correct behaviour when the
runtime collects logs, so there is no file to search. (The `app.log` in the
working tree is left over from Run B; `run_drill.py` now compares the file's mtime
against the start of the drill and says so explicitly instead of reporting a
misleading "not found".)

The same structured envelope is visible in `docker compose logs web`:

```json
{"ts": "2026-09-20T16:14:07.469Z", "level": "INFO", "logger": "app.access", "message": "http_request", "service": "cloud-ops-mini-stack", "version": "1.1.0", "request_id": "22ec9746948b48bf", "http": {"method": "GET", "endpoint": "/healthz/", "path": "/healthz/", "status": 200}, "duration_ms": 10.207, "client_ip": "127.0.0.1", "user_agent": "curl/8.14.1", "user_id": null}
```

Same logger, same `request_id`, same fields as Run A — the pipeline is identical,
only the sink differs.

### An environment caveat, for anyone reproducing this

Docker Engine 29 defaults to the nftables firewall backend, and the WSL2 kernel
shipped by the (now frozen) `wsl_update_x64.msi` is 5.10.16 from 2021, whose
nftables cannot create a NAT chain:

```
failed to add jump rules to ipv4 NAT table:
CHAIN_ADD failed (No such file or directory): chain PREROUTING
```

Pinning `iptables` to the legacy xtables path fixes it
(`update-alternatives --set iptables /usr/sbin/iptables-legacy`). Nothing about
this project depends on it — it is a property of running a 2026 Docker on a 2021
kernel, and it is recorded here because the error message points at the network
stack rather than at the kernel version.

---

## What is *not* covered

| Gap | Why | Impact |
|---|---|---|
| Alert delivery | No Alertmanager in this stack | Alerts **fire** and are visible at `/alerts`; nothing is notified. See [KNOWN_ISSUES #8](KNOWN_ISSUES.md) |
| Sentry event delivery | No DSN configured in this environment | The integration is unit-tested; the network hop is not. See [KNOWN_ISSUES #12](KNOWN_ISSUES.md) |
| Log *shipping* from the container | No collector in this stack | The container emits structured JSON to stdout (verified in Run C); nothing forwards it to a store. Locally, `docker compose logs` is the only reader |
| Detection latency behind a load balancer | Nothing sits in front of the service | The 15 s scrape interval is included in Run B; network latency and proxy buffering are not |
| Multi-worker behaviour | Single worker by design | See [KNOWN_ISSUES #1](KNOWN_ISSUES.md) |
| Disk exhaustion | No `node_exporter` | See [KNOWN_ISSUES #9](KNOWN_ISSUES.md) |

Stating the gaps is the point of the log. A drill log that only lists successes
tells the reader nothing about where to look next.

## Cadence

Run the automated drills before every release that touches the observability path
(`ops/`, `settings.py`, alert rules, dashboards). The whole set takes under five
seconds and the alternative is discovering that a rename broke detection during an
incident.

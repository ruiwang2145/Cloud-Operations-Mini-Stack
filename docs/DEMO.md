# Demo script

A five-minute walkthrough that shows the service working, the monitoring working,
and a failure being detected. Written so that someone else can run it without
having read the code.

## Before you start

```bash
git clone https://github.com/ruiwang2145/Cloud-Operations-Mini-Stack.git
cd Cloud-Operations-Mini-Stack

docker compose up -d --build
docker compose --profile demo up -d      # optional: generates traffic
```

Wait for all four services to be healthy:

```bash
docker compose ps
# web, db, prometheus, grafana all "Up"; db "Up (healthy)"
```

| Service | URL |
|---|---|
| API | http://localhost:8000/api/tasks/ |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (admin / admin, anonymous viewer enabled) |

Seed some data so the graphs are not empty:

```bash
docker compose exec web python manage.py seed_tasks --count 40
```

---

## 1. It is a real service (60 seconds)

```bash
curl -sS http://localhost:8000/healthz/ | python -m json.tool
curl -sS http://localhost:8000/readyz/ | python -m json.tool
curl -sS http://localhost:8000/api/tasks/ | python -m json.tool | head -30
```

**Say:** liveness does no I/O on purpose, readiness checks the database and returns
503 when it is unusable. They are separate because they have opposite failure
behaviours — restart versus remove-from-rotation — and a combined probe would turn
a database outage into a restart loop across every worker.

```bash
# Writes need authentication; reads do not.
curl -sS -X POST http://localhost:8000/api/tasks/ \
  -H 'Content-Type: application/json' -d '{"title":"demo"}' | python -m json.tool
```

**Say:** every error comes back in one envelope, and it carries the request id that
ties the response to the exact log line.

---

## 2. One command proves it works (30 seconds)

```bash
python scripts/smoke_test.py
```

Eleven checks, each with its own timing. This is the script that answers "is the
deployment correct?" as opposed to "does the code work?" — a test suite proves the
latter and says nothing about the port, the environment or the migrations.

---

## 3. Structured logs (45 seconds)

```bash
curl -sS http://localhost:8000/api/tasks/?page=1 > /dev/null
docker compose logs --tail=5 web
```

One JSON object per line, with `ts`, `level`, `logger`, `message`, `service`,
`version`, `request_id`, and an `http` object carrying method, endpoint, status and
duration.

**Say:** `message` is an event *name* — `http_request`, not "handled a request" —
because an event name can be counted and a sentence cannot. And the `endpoint`
label is the URL *pattern* (`/api/tasks/{pk}/`), not the path: labelling by raw path
means one time series per task id, which is how a Prometheus server dies slowly.

---

## 4. A failure is detected end to end (90 seconds)

This is the part worth the demo.

```bash
# Fault injection is enabled in the compose stack only.
python scripts/run_drill.py --drill unhandled-exception
```

It injects a real `RuntimeError`, polls `/metrics`, and reports:

| | |
|---|---|
| Detected by | `app_errors_total{error_type="RuntimeError"}` |
| Time to detect | milliseconds locally; one scrape interval in production |
| Counter delta | exactly 1 |
| Log correlation | request id matched |

**Say these three things, because each was a decision that could have been wrong:**

1. Django wraps every middleware in `convert_exception_to_response`, so an
   exception in a view never crosses a middleware boundary. A `try/except` around
   `get_response` would never fire — detection goes through the `process_exception`
   hook instead.
2. Two observers see the same failure, so the request carries a flag and the
   counter rises by exactly one. The drill asserts it.
3. The `X-Request-ID` the client sent appears verbatim in the server's log, so a
   user's bug report is one query away from the stack trace.

Then show the log line:

```bash
docker compose logs web | grep unhandled_exception | tail -1
```

---

## 5. The SLO is a number, not a vibe (60 seconds)

```bash
curl -sS http://localhost:8000/api/slo/ | python -m json.tool
```

```json
{
  "window": "30d",
  "source": "prometheus",
  "availability": {"sli": 0.9991, "objective": 0.995, "met": true,
                   "error_budget_remaining": 0.82, "burn_rate": 0.18},
  "latency": {"sli": 0.994, "objective": 0.99, "threshold_ms": 300},
  "status": "healthy"
}
```

**Say:** availability is `1 - (5xx / total)` over 30 days. 4xx deliberately does not
count — a rejected write is the caller's mistake, and counting it would measure who
is calling rather than whether the service works. Probe traffic is excluded
everywhere, because a health-check loop that never fails would drag the measured
error rate towards zero regardless of what users experienced.

The **error budget** is the part that makes this real: 99.5% permits 0.5% of
requests to fail over 30 days, and when it is spent the agreed policy says feature
work stops. `docs/SLO.md` has the policy table.

---

## 6. The dashboards (60 seconds)

Open http://localhost:3000 → **Cloud Ops · Service Overview**.

Six stat panels across the top — availability, error budget remaining, burn rate,
request rate, 5xx rate, scrape up — then request rate by endpoint, 5xx by endpoint,
latency percentiles and latency compliance against the objective.

**Say:** both dashboards are provisioned from JSON in the repository, so they are
version controlled and a rebuilt container comes back with exactly the same views.
`ops/tests/test_dashboards.py` extracts every PromQL expression and asserts that
each metric and label it references is actually exported — because a panel querying
a renamed metric renders as *empty*, which on a quiet service is indistinguishable
from "no traffic".

Then open **Cloud Ops · API & Latency Detail** and point at the `endpoint` variable
at the top.

---

## 7. What happens when it breaks (60 seconds)

Open http://localhost:9090/alerts.

**Say:** nine alert rules, generated from the SLO definition by
`scripts/render_rules.py`. Two burn-rate windows — 14.4× over an hour to page,
6× over six hours to ticket — because a single short window pages on every blip and
a single long window stays quiet through a fast outage.

```bash
python scripts/render_rules.py --check      # fails if the rules drifted
```

Every alert carries a `runbook:` annotation, and a test fails the build if that
runbook does not exist. The runbooks are in `docs/runbooks/` and each one is
structured symptoms → impact → first five minutes → diagnose → mitigate → verify →
follow-up.

Then open `docs/KNOWN_ISSUES.md` and point at the honest gaps: no Alertmanager, no
disk alert, one worker because multi-worker metrics would be wrong, no restore
drill. **A project that lists what it does not do is more credible than one that
does not.**

---

## 8. Clean up

```bash
docker compose --profile demo down
docker compose down -v      # also removes the volumes (database, metrics, dashboards)
```

---

## If something does not work

| Symptom | Check |
|---|---|
| Grafana shows no data | `docker compose ps prometheus`; then http://localhost:9090/targets |
| A panel is empty | The metric may have been renamed — `python -m pytest ops/tests/test_dashboards.py` |
| `/readyz/` returns 503 | `docker compose logs db`; see `docs/runbooks/database-unreachable.md` |
| Drill reports `404` from `/boom/` | `ENABLE_FAULT_ENDPOINTS` is not set to `true` |
| Ports already in use | `docker compose down`, and stop any local `runserver` |

## Running without Docker

Two options, depending on what needs demonstrating.

### Just the service

```bash
bash scripts/bootstrap.sh          # or: powershell -File scripts\bootstrap.ps1
.venv/bin/python manage.py runserver
```

SQLite, no Prometheus, no Grafana. `/api/slo/` falls back to the in-process
registry and says so in `window_source` — which is itself worth showing, because it
is the honest-degradation decision in action.

### The service plus real Prometheus and Grafana

Both ship standalone binaries, so the alerting and dashboard path can be
demonstrated with no container runtime, no administrator rights and no reboot —
and this is how the results in [incident-drills.md](incident-drills.md) were
produced. Full recipe: [native-monitoring.md](native-monitoring.md).

```bash
promtool check config monitoring/prometheus/prometheus.local.yml
prometheus --config.file=monitoring/prometheus/prometheus.local.yml \
           --storage.tsdb.path=/tmp/prometheus-data --web.enable-lifecycle
```

**What this demonstrates that the container path cannot be shown to:** the alert
rules evaluating against live data at `/alerts`, and every dashboard panel
rendering real numbers. Steps 4 to 7 above work exactly the same way, with
`localhost:9090` and `localhost:3000` in place of the compose service names.

### Drilling the readiness failure

The one drill that cannot be run from outside the service. `manage.py runserver`
refuses to start without a database, so use the launcher that skips startup
checks — the same thing gunicorn does, which is why a production container can be
up-but-not-ready:

```bash
DATABASE_URL=postgresql://bad:bad@127.0.0.1:59999/bad \
  .venv/bin/python scripts/serve_for_drills.py

curl -o /dev/null -w 'healthz %{http_code}\n' http://localhost:8000/healthz/   # 200
curl -sS http://localhost:8000/readyz/                                         # 503
curl -sS http://localhost:8000/metrics | grep app_readiness_failures_total
```

The point of the demo is the first line: **liveness stays 200 while readiness
fails.** If liveness also touched the database, the orchestrator would restart
every healthy worker in a loop during a database outage.

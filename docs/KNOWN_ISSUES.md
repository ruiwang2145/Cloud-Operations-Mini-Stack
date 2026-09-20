# Known issues

Things this service does not do, does imperfectly, or does in a way that will
surprise you. Written down because the alternative is that every one of them is
rediscovered during an incident — and because "we know about that" is a very
different answer from "we have never seen that before".

**Status key:** 🔴 open · 🟡 accepted (understood, deliberately not fixed) · 🟢 fixed

---

## 1. Metrics are per-worker under gunicorn 🔴

**Symptom.** Request counts are far lower than the actual traffic, and they jump
unpredictably between scrapes.

**Cause.** `prometheus_client` keeps counters in process memory. With N gunicorn
workers there are N independent registries. Prometheus scrapes whichever worker
answers the connection, so it sees one worker's partial view, changing at random.

**Current mitigation.** The container runs **one worker** (`--workers 1` in the
`Dockerfile`). Metrics that look plausible and are wrong are worse than no metrics
at all, so correctness wins over parallelism here.

**Proper fix.** `prometheus_client`'s multiprocess mode:

```bash
PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus
```

`scripts/docker-entrypoint.sh` already clears and creates the directory when that
variable is set, and `django-prometheus` switches to `MultiProcessCollector`
automatically. **But it is not a drop-in change**: in multiprocess mode gauges are
aggregated across processes by summation, so `app_slo_availability_ratio` from
three workers reports `3.0`. The SLO gauges would have to move out of the app
(a Prometheus recording rule, or a separate exporter) before this can be enabled.

---

## 2. `django-prometheus` metric names end in `_total_total` 🟡

**Symptom.** A dashboard panel is empty and the metric name looks right.

**Cause.** `django-prometheus` names its counter
`django_http_responses_total_by_status_view_method`, and `prometheus_client`
appends `_total` to counters. The exported series is therefore
`django_http_responses_total_by_status_view_method_total`.

**Mitigation.** The real names are asserted in
`ops/tests/test_probes.py::TestMetricsEndpoint`, and the dashboards are checked
against the live `/metrics` output by `ops/tests/test_dashboards.py`. Renaming
either one fails the build.

**Status.** Not fixable — it is the library's naming. Documented because it is the
kind of thing that costs an hour.

---

## 3. `/metrics` has no trailing slash 🔴

**Symptom.** Prometheus reports the target as down while the service is healthy.

**Cause.** The view is registered at `metrics` (no slash), and Django's
`APPEND_SLASH` appends slashes to match patterns — it does not strip them. So
`/metrics` works and `/metrics/` is a 404.

**Mitigation.** `monitoring/prometheus/prometheus.yml` uses `metrics_path: /metrics`.
`scripts/smoke_test.py` asserts that `/metrics/` returns 404, so the behaviour
cannot change silently.

**Why not just register both.** Because two routes for one endpoint means two
values for the `endpoint` metric label, and a dashboard that sums them would
double-count. One canonical path is better.

---

## 4. Neon serverless cold starts can flap the readiness probe 🟡

**Symptom.** Occasional `503` from `/readyz/` immediately after a period of
inactivity, recovering on the next attempt. `ReadinessCheckFailing` fires and then
clears itself.

**Cause.** Neon suspends idle compute. The first connection after suspension has to
wake the database, which can exceed the 5-second `DB_CONNECT_TIMEOUT`.

**Mitigation.** `DB_CONNECT_TIMEOUT` is configurable; raising it to 10 s trades
slower failure detection for fewer false positives.

**Proper fix.** Use the pooled connection endpoint, or keep the compute warm.
Both are provider-specific configuration rather than application changes.

**Why it is not "fixed" by retrying in the probe.** A readiness probe that retries
is a readiness probe that lies about latency. If the dependency is slow, the
instance is genuinely not ready — the honest answer is to say so and let the load
balancer route elsewhere.

---

## 5. SQLite under concurrent writes returns "database is locked" 🟡

**Symptom.** In local development with the load generator running, writes fail
intermittently with `OperationalError: database is locked`.

**Cause.** SQLite allows one writer at a time. The default `DATABASE_URL` is
SQLite so that `bootstrap.sh` works with no server, and the load generator plus
`runserver`'s threads is enough concurrency to hit it.

**Mitigation.** Use PostgreSQL for anything involving concurrency — which is what
`docker compose up` provides. SQLite is for the single-user local path only.

**Why not switch the default to PostgreSQL.** Because the first-run experience
would then require Docker, and a project that cannot be started without a
container runtime is a project reviewers do not start.

---

## 6. `LocMemCache` is per-process 🔴

**Symptom.** With multiple workers, `/api/slo/` returns reports with different
`generated_at` timestamps, so different requests get slightly different answers.

**Cause.** The SLO report is cached in `LocMemCache`, which lives in the process.
N workers means N caches, so the 15-second cache does not deduplicate across them.

**Mitigation.** With one worker (the current default) there is exactly one cache.
The reports are read-only and idempotent, so a stale answer is not incorrect —
it is just up to 15 seconds old.

**Proper fix.** A shared cache (Redis). Deliberately not done: adding a Redis
dependency to deduplicate a 15-second read-only cache costs more operationally
than it saves.

---

## 7. The anonymous API throttle is also per-process 🔴

**Symptom.** The effective anonymous rate limit is `API_ANON_THROTTLE × workers`,
not `API_ANON_THROTTLE`.

**Cause.** DRF's `AnonRateThrottle` uses the same `LocMemCache` by default.

**Mitigation.** Single worker. The default of `120/min` is a runaway-client
backstop, not a security control.

**Proper fix.** A shared cache. See #6 — same fix, same reason for deferring it.

---

## 8. Alerts are visible but not delivered 🔴

**Symptom.** A critical alert fires and nobody is told.

**Cause.** There is no Alertmanager in this stack. Firing alerts appear at
`http://localhost:9090/alerts` and nowhere else.

**Mitigation.** None. This is a real gap.

**Proper fix.** Add Alertmanager with a routing tree, and point the alert rules at
it. The rules already carry `severity` labels, which is what a routing tree
matches on — so the work is configuration, not rewriting.

---

## 9. No disk-space alert 🔴

**Symptom.** A volume fills up and the first sign is a database write failure.

**Cause.** Disk metrics come from `node_exporter`, which this stack does not run.

**Mitigation.** [disk-and-log-growth.md](runbooks/disk-and-log-growth.md) documents
the manual check.

**Proper fix.** Add `node_exporter` and alert on
`node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.15`.

---

## 10. Migrations run in the entrypoint, which races with more than one replica 🔴

**Symptom.** During a rolling deploy with two or more web replicas, one crashes
with a duplicate-object or "relation already exists" error.

**Cause.** Every replica runs `manage.py migrate` on startup, concurrently.

**Mitigation.** `RUN_MIGRATIONS=false` on the web service, and run migrations as a
separate one-shot job first. Documented in
[deploy-and-rollback.md](runbooks/deploy-and-rollback.md).

**Why it is not the default.** With one replica — the current deployment shape —
the entrypoint version is simpler and cannot be forgotten. The switch exists so
that scaling out is a configuration change rather than a rewrite.

---

## 11. Fault-injection endpoints are a real risk if enabled in production 🟡

**Symptom.** Anyone can generate a 500 by requesting `/boom/`.

**Cause.** The endpoints exist so the incident drills can prove that error capture
and alerting work end to end.

**Mitigation.** `ENABLE_FAULT_ENDPOINTS` defaults to `False`, and a disabled
endpoint returns **404, not 403** — a 403 confirms the endpoint exists and invites
someone to keep trying. `ops/tests/test_fault_endpoints.py` includes a test that
reads `settings.py` and fails if the default is ever flipped.

`docker-compose.yml` sets it to `true` because the compose stack is a demo whose
whole purpose is to show the drills working. That is the one place it is on.

---

## 12. Sentry is configured but has never received a real event in this environment 🟡

**Symptom.** `/sentry-debug/` returns 500 and Sentry shows nothing.

**Cause.** `SENTRY_DSN` is empty by default. Sentry is optional: the service is
fully functional without it, and requiring a third-party account to run the
project would be a bad first-run experience.

**Mitigation.** Set `SENTRY_DSN` to test the path. The integration code, the
sampling rates, the release tagging and the probe-noise filter
(`ops/sentry_filters.py`) are all in place and unit-tested; what is untested is
the network hop.

**Honest statement.** The drill results in
[incident-drills.md](incident-drills.md) measure detection through the **metrics
and logs**, not through Sentry, because no DSN was configured when they ran.

---

## 13. Log timestamps are UTC and the log file is local to the process 🟡

**Symptom.** Log timestamps do not match the wall clock on the developer's
machine, and in a container the log file does not exist at all.

**Cause.** Everything is UTC on purpose — correlating a log line with a metric
sample, a Sentry event and an alert requires every source to agree on the clock,
and local time is a presentation concern. `LOG_TO_FILE` defaults to `true` for
local development; the container image sets it to `false` so logs go to stdout and
the log driver owns rotation.

**Mitigation.** `LOG_FORMAT=console` for a readable local terminal;
`LOG_TO_FILE=false` in any container.

---

## 14. `send_default_pii` was on, and is now off 🟢

**Symptom.** None observable — which is the problem with this class of bug.

**Cause.** The Sentry SDK was initialised with `send_default_pii=True`. That
attaches request headers and bodies to every event, and those routinely carry
authorisation tokens and personal data. Sending them to a third party should be an
explicit decision, not a default.

**Fix.** `SENTRY_SEND_PII` defaults to `False` and is documented in `.env.example`.

**What is still open.** The flag is off, but there is no scrubbing of PII that
appears inside *exception messages* — a database error that quotes a row is still
a database error that quotes a row. 🔴

---

## 15. No backup or restore procedure 🟡

**Symptom.** Not yet observable, because no restore has ever been attempted.

**Cause.** The stack has no backup job. Neon provides point-in-time recovery on its
paid tiers; the local PostgreSQL container has only its volume.

**Honest statement.** "We have backups" and "we have restored from a backup" are
different claims. Only the second one is evidence, and this project cannot make it.
A restore drill would be the next thing to build.

---

## Not an issue, but worth knowing

### The SLO reporter silently degrades

When Prometheus is unreachable, `/api/slo/` falls back to the in-process registry
and reports `"window_source": "process_lifetime"`. It does **not** fail, because a
monitoring endpoint that returns 500 when monitoring is down is useless exactly
when it is needed. `SloReporterDegraded` fires so the degradation is visible. Read
the `window_source` field before acting on a number.

### The `no_data` status is not success

A brand-new deployment reports `"status": "no_data"` rather than 100%
availability. Returning a perfect score for a service with no traffic would hide a
broken scrape configuration behind a green number.

### `4xx` never counts against availability

Deliberate — see [SLO.md](SLO.md). The consequence is that a bug that returns 400
instead of 500 is invisible to the SLO. Per-endpoint correctness checks would catch
it; a service-level availability objective cannot.

# ☁️ Cloud Operations Mini Stack

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Django](https://img.shields.io/badge/django-5.2-092E20.svg)](https://www.djangoproject.com/)
[![DRF](https://img.shields.io/badge/DRF-3.18-A30000.svg)](https://www.django-rest-framework.org/)
[![PostgreSQL](https://img.shields.io/badge/postgresql-17-336791.svg)](https://www.postgresql.org/)
[![Prometheus](https://img.shields.io/badge/prometheus-3.14-E6522C.svg)](https://prometheus.io/)
[![Grafana](https://img.shields.io/badge/grafana-13.2-F46800.svg)](https://grafana.com/)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Sentry](https://img.shields.io/badge/sentry-optional-362D59.svg)](https://sentry.io/)
[![Tests](https://img.shields.io/badge/tests-136%20passing-brightgreen.svg)](#testing)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A Django REST API that is **operated** as well as written: health probes, RED
metrics, structured logs, a defined availability SLO with an error budget, alert
rules generated from that definition, Grafana dashboards, runbooks, and a scripted
incident drill that proves detection actually works.

The business logic is deliberately tiny — one model, one CRUD API. The interesting
part is everything around it, because that is where services actually fail.

```bash
docker compose up -d --build
docker compose --profile demo up -d      # optional: generate traffic
python scripts/smoke_test.py             # 11 checks against the running stack
python scripts/run_drill.py --drill unhandled-exception
```

| | |
|---|---|
| API | http://localhost:8000/api/tasks/ |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

---

## What this demonstrates

| Capability | Where to look |
|---|---|
| **REST API design** — versionless CRUD, one error envelope, allow-listed ordering, throttling | `tasks/` |
| **Health checking** — liveness vs readiness, and why they must be separate | `ops/views.py`, [runbook](docs/runbooks/database-unreachable.md) |
| **Metrics** — RED metrics with bounded label cardinality | `ops/metrics.py`, `ops/endpoints.py` |
| **Structured logging** — JSON schema, request-id correlation, log rotation | `ops/logging.py` |
| **SLO engineering** — SLIs, error budget, burn rate, multi-window alerting | [`docs/SLO.md`](docs/SLO.md) |
| **Alerting** — rules generated from the SLO definition so they cannot drift | `scripts/render_rules.py` |
| **Dashboards** — provisioned from JSON, asserted against the real metric names | `monitoring/grafana/`, `ops/tests/test_dashboards.py` |
| **Containerisation** — multi-stage build, non-root, healthcheck, entrypoint | `Dockerfile`, `docker-compose.yml` |
| **Automation** — one-command setup, smoke test, incident drills | `scripts/` |
| **Operational writing** — runbooks, known issues, incident drills, post-mortem template | [`docs/`](docs/) |
| **Testing** — 136 tests including the guarantees above | `ops/tests/`, `tasks/tests/` |

---

## Architecture

```
                      ┌──────────────────┐
                      │   UptimeRobot    │  external, every 5 min
                      └────────┬─────────┘
                               │ GET /healthz/
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  web  ·  gunicorn + Django 5.2                               │
│                                                              │
│   middleware (outermost first)                               │
│     PrometheusBeforeMiddleware    framework timer            │
│     RequestIdMiddleware           correlation id             │
│     ObservabilityMiddleware       access log + RED metrics   │
│     Security / sessions / csrf / auth                        │
│     PrometheusAfterMiddleware     framework timer            │
│                                                              │
│   ops/     probes · metrics · logging · SLO reporter         │
│   tasks/   the only business model, exposed over DRF         │
└───────────┬──────────────────────────┬───────────────────────┘
            │ /metrics (scrape 15s)    │ SQL
            ▼                          ▼
   ┌─────────────────┐        ┌──────────────────┐
   │  prometheus     │        │  PostgreSQL 17   │
   │  + alert rules  │        │  (Neon in prod)  │
   └────────┬────────┘        └──────────────────┘
            │ query
            ▼
   ┌─────────────────┐        ┌──────────────────┐
   │  grafana        │        │  Sentry (opt-in) │
   │  2 dashboards   │        │  errors + APM    │
   └─────────────────┘        └──────────────────┘
```

The reasoning behind every box and every ordering decision is in
[`docs/architecture.md`](docs/architecture.md).

---

## Quick start

### The whole stack (Docker)

```bash
git clone https://github.com/ruiwang2145/Cloud-Operations-Mini-Stack.git
cd Cloud-Operations-Mini-Stack
docker compose up -d --build

docker compose exec web python manage.py seed_tasks --count 40
python scripts/smoke_test.py
```

Requires Docker with the Compose plugin. Nothing else.

### Local development (no Docker)

```bash
bash scripts/bootstrap.sh                 # Linux, macOS, Git Bash
powershell -File scripts\bootstrap.ps1    # Windows
```

Creates a virtualenv, installs dependencies, writes a `.env` with a generated
`SECRET_KEY`, applies migrations and runs Django's system checks. SQLite by
default, so there is no database to install.

```bash
.venv/bin/python manage.py runserver
.venv/bin/python scripts/smoke_test.py
.venv/bin/python -m pytest
```

### Without Docker, with the full monitoring stack

Prometheus and Grafana both ship standalone binaries, so the alerting and
dashboard path can be verified without a container runtime — no administrator
rights and no reboot. This is how the results in
[`docs/incident-drills.md`](docs/incident-drills.md) were produced.

```bash
# check the configs before running them
promtool check config monitoring/prometheus/prometheus.local.yml
promtool check rules  monitoring/prometheus/alert_rules.yml

# then start Prometheus against the local instance
prometheus --config.file=monitoring/prometheus/prometheus.local.yml \
           --storage.tsdb.path=/tmp/prometheus-data --web.enable-lifecycle
```

Full recipe, including Grafana provisioning:
[`docs/native-monitoring.md`](docs/native-monitoring.md).

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/tasks/` | List, paginated. `?done=true`, `?priority=high`, `?ordering=title` |
| `POST` | `/api/tasks/` | Create (authentication required) |
| `GET` | `/api/tasks/{id}/` | Retrieve |
| `PUT` / `PATCH` | `/api/tasks/{id}/` | Update |
| `DELETE` | `/api/tasks/{id}/` | Delete |
| `GET` | `/api/tasks/stats/` | Aggregate counts in one query |

### Operational endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz/` | Liveness. No I/O — if this fails, restart the process |
| `GET` | `/readyz/` | Readiness. Checks the database, returns 503 when unusable |
| `GET` | `/metrics` | Prometheus scrape target (**no trailing slash**) |
| `GET` | `/version/` | Which build is answering |
| `GET` | `/api/slo/` | Availability and latency SLO report |
| `GET` | `/boom/`, `/sentry-debug/` | Fault injection. 404 unless `ENABLE_FAULT_ENDPOINTS=true` |

### Error contract

Every failure, from every layer, returns the same shape:

```json
{
  "error": {
    "type": "ValidationError",
    "status": 400,
    "detail": {"title": ["Title must not be blank."]},
    "request_id": "9f2c1a4b7e0d3856"
  }
}
```

`request_id` is the useful part: it maps to the exact access log line, the exact
metric increment and the exact Sentry event for that request.

---

## Observability

### Four layers, each answering a different question

| Layer | Tool | Question |
|---|---|---|
| **Health checks** | `/healthz/`, `/readyz/`, UptimeRobot | Is it up, and should it receive traffic? |
| **Metrics** | Prometheus + `django-prometheus` | What has it been doing? |
| **Structured logs** | JSON to stdout, correlated by request id | What exactly happened? |
| **Error tracking** | Sentry (opt-in) | What did the exception look like? |

### Service metrics

Beyond the framework metrics from `django-prometheus`, the service exports the
series the SLOs and alerts are written against:

| Metric | Type | Labels |
|---|---|---|
| `app_http_requests_total` | counter | `method`, `endpoint`, `status_class` |
| `app_http_request_duration_seconds` | histogram | `method`, `endpoint` |
| `app_errors_total` | counter | `endpoint`, `error_type` |
| `app_readiness_failures_total` | counter | `dependency` |
| `app_slo_*` | gauge | — |
| `app_build_info` | info | `service`, `version`, `environment` |

**`endpoint` is the URL pattern, not the path.** `/api/tasks/1/` and
`/api/tasks/2/` are the same series. Labelling by raw path creates one time series
per task id, which is how a Prometheus server dies slowly — and it is not a
hypothetical: `scripts/run_drill.py --drill unknown-path-scan` requests 200
distinct URLs and asserts that exactly one series is produced.

### The SLO

| | Objective | Window |
|---|---|---|
| Availability | 99.5% of requests succeed | 30 days |
| Latency compliance | 99% of requests under 300 ms | 30 days |

```bash
curl -sS http://localhost:8000/api/slo/ | python -m json.tool
```

```json
{
  "window": "30d", "source": "prometheus",
  "availability": {"sli": 0.9991, "objective": 0.995, "met": true,
                   "error_budget_remaining": 0.82, "burn_rate": 0.18},
  "latency": {"sli": 0.994, "objective": 0.99, "met": true, "threshold_ms": 300},
  "status": "healthy"
}
```

Three decisions worth knowing, all argued in [`docs/SLO.md`](docs/SLO.md):

- **4xx does not count as an error.** A rejected write is the caller's mistake.
  Counting it would measure who is calling, not whether the service works.
- **Probe traffic is excluded everywhere.** A health-check loop that never fails
  would otherwise drag the measured error rate towards zero regardless of what
  users experienced.
- **`no_data` is not success.** A service with no traffic reports `no_data` rather
  than 100%, because a perfect score would hide a broken scrape configuration.

The **error budget** is what makes the objective actionable: 99.5% permits 0.5% of
requests to fail over 30 days, and when it is spent the agreed policy says feature
work stops. That policy is a table in `docs/SLO.md`.

---

## Alerting

Nine rules across three groups, with two burn-rate windows:

| Alert | Severity | Fires when |
|---|---|---|
| `ServiceDown` | critical | Prometheus cannot scrape for 2 min |
| `AvailabilityBudgetBurnFast` | critical | Burning at 14.4× the sustainable rate over 1 h |
| `AvailabilityBudgetBurnSlow` | warning | Burning at 6× over 6 h |
| `HighErrorRate` | critical | >5% of requests are 5xx for 5 min |
| `ErrorBudgetExhausted` | warning | Availability is below the objective |
| `LatencyObjectiveBreach` | warning | <99% of requests under 300 ms for 10 min |
| `ReadinessCheckFailing` | warning | A dependency failed a readiness check |
| `SloReporterDegraded` | warning | The SLO reporter fell back to the local registry |
| `NoTraffic` | warning | No application traffic for 30 min |

**The rules are generated, not written by hand:**

```bash
python scripts/render_rules.py            # regenerate from the SLO definition
python scripts/render_rules.py --check    # fails if they have drifted
```

The objective appears in three places — the API report, the documentation and the
rules — and the third is the one that matters at 03:00 and the one most likely to
be stale. Generating it removes the possibility rather than relying on discipline.
`ops/tests/test_alert_rules.py` fails the build on drift.

Every alert carries a `runbook:` annotation, and a test fails if that runbook does
not exist.

---

## Dashboards

Two provisioned dashboards, version controlled as JSON:

- **Cloud Ops · Service Overview** — availability, error budget remaining, burn
  rate, request rate, 5xx rate, scrape status; then per-endpoint request and error
  rates, latency percentiles, and latency compliance against the objective.
- **Cloud Ops · API & Latency Detail** — per-endpoint breakdown, framework-level
  status distribution, p95 by Django view, cumulative latency distribution, and
  SLO state over time.

`ops/tests/test_dashboards.py` extracts every PromQL expression from the JSON and
asserts that each metric and label it references is actually exported by
`/metrics`. A panel querying a renamed metric renders as *empty*, which on a quiet
service is indistinguishable from "no traffic" — so the coupling is checked
mechanically instead of by eye.

---

## Operations

### Runbooks

`docs/runbooks/` — each one structured **symptoms → impact → first five minutes →
diagnose → mitigate → verify → follow-up**:

| Runbook | Use when |
|---|---|
| [service-down](docs/runbooks/service-down.md) | Unreachable, or `ServiceDown` / `NoTraffic` |
| [high-error-rate](docs/runbooks/high-error-rate.md) | Error rate elevated or budget burning |
| [high-latency](docs/runbooks/high-latency.md) | Requests slow |
| [database-unreachable](docs/runbooks/database-unreachable.md) | Readiness failing |
| [deploy-and-rollback](docs/runbooks/deploy-and-rollback.md) | Shipping, or a bad deploy |
| [metrics-and-logs](docs/runbooks/metrics-and-logs.md) | You need to see what it is doing |
| [disk-and-log-growth](docs/runbooks/disk-and-log-growth.md) | A volume is filling up |

The principle throughout: **mitigate before you diagnose.** Restarting a wedged
process restores service; understanding why can wait thirty minutes. A runbook that
starts with "open a debugger" turns a five-minute outage into a fifty-minute one.

### Incident drills

A monitoring stack that has never observed a failure is a hypothesis.

```bash
python scripts/run_drill.py --drill unhandled-exception
python scripts/run_drill.py --drill sentry-path
python scripts/run_drill.py --drill unknown-path-scan
python scripts/run_drill.py --drill readiness-failure
```

Each injects one failure, polls the metrics until it appears, measures the time to
detection, and correlates the log line by request id.

The drills were also run against a **real Prometheus and Grafana**, scraping every
15 seconds, and the results are recorded in
[`docs/incident-drills.md`](docs/incident-drills.md). That run answered the
questions the scripts cannot:

| Verified | Result |
|---|---|
| Every rule expression executes against real data | all 9 rules report `health=ok` |
| Alerts fire when the SLO is breached, and only then | `AvailabilityBudgetBurnFast` and `HighErrorRate` **firing** at 23.3× and 15.5% |
| Every dashboard panel renders | **27 of 27** panel queries returned data |
| Readiness fails closed while liveness survives | `/readyz/` 503 in exactly `DB_CONNECT_TIMEOUT`; `/healthz/` 200 throughout |

The log also records what the run did **not** cover: container behaviour, alert
delivery, and anything behind a load balancer.

### Known issues

[`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) lists 15 things this service does not
do, does imperfectly, or does in a way that will surprise you — with symptom, cause,
mitigation and the proper fix for each.

It includes the honest ones: no Alertmanager, so alerts are visible but not
delivered; no disk alert; one gunicorn worker because multi-worker metrics would be
wrong; no backup restore drill; `LocMemCache` means the throttle is per-process.

A project that lists what it does not do is more credible than one that does not.

---

## Automation

| Script | Purpose |
|---|---|
| `scripts/bootstrap.sh` / `.ps1` | One-command setup. Idempotent, both platforms |
| `scripts/smoke_test.py` | 11 end-to-end checks against a running instance. `--json` for CI |
| `scripts/run_drill.py` | Incident drills with measured detection latency |
| `scripts/render_rules.py` | Generate alert rules from the SLO definition |
| `scripts/loadgen.py` | Realistic traffic mix so the dashboards are not empty |
| `scripts/serve_for_drills.py` | Serve without startup database checks, so the readiness drill is possible |
| `scripts/gen_secret.py` | Fresh `SECRET_KEY` |
| `scripts/docker-entrypoint.sh` | Wait for the database, migrate, collectstatic, exec |

```bash
python scripts/smoke_test.py
```

```
  [PASS] liveness returns 200 and reports ok                              12.0 ms
  [PASS] readiness reports every dependency as reachable                   5.1 ms
  [PASS] metrics endpoint exports every required family                    9.4 ms
  [PASS] metrics is served without a trailing slash                        7.1 ms
  [PASS] anonymous writes are refused with the standard error envelope     4.2 ms
  ...
  11 passed, 0 failed  (73 ms)
```

A test suite proves the code works in a test database with a test client. It says
nothing about the port, the environment, the proxy or the migrations — which are
the failure modes that actually happen on a deploy. That gap is what this fills.

---

## Testing

```bash
python -m pytest              # 136 tests
python -m ruff check .        # lint
python scripts/render_rules.py --check
```

136 tests, and the interesting ones assert the *operational* guarantees rather than
the CRUD:

| Test | Guarantee |
|---|---|
| `ops/tests/test_endpoints.py` | 60 distinct task URLs produce **one** metric series |
| `ops/tests/test_middleware.py` | An unhandled exception is counted exactly once, with its type |
| `ops/tests/test_probes.py` | Liveness survives a database outage; `/readyz/` fails closed |
| `ops/tests/test_slo.py` | Burn-rate arithmetic, including the division-by-zero edges |
| `ops/tests/test_dashboards.py` | Every dashboard query references a metric that exists |
| `ops/tests/test_alert_rules.py` | Alert rules match the SLO definition; runbooks exist; both scrape configs load the same rules |
| `ops/tests/test_logging.py` | Log output stays valid JSON when messages contain quotes |
| `ops/tests/test_fault_endpoints.py` | Fault endpoints default to off |
| `tasks/tests/test_api.py` | Query count does not grow with the number of rows |

---

## Project structure

```
.
├── my_cloudapp/                 Django project
│   ├── settings.py              environment-driven, all decisions commented
│   ├── settings_test.py         test overrides
│   └── urls.py
├── ops/                         everything that makes the service operable
│   ├── views.py                 probes, /metrics, /api/slo/, fault injection
│   ├── middleware.py            request id + access log + RED metrics
│   ├── logging.py               JSON formatter, context filter
│   ├── metrics.py               the metric definitions
│   ├── endpoints.py             label cardinality guard
│   ├── exceptions.py            one error envelope, one error metric
│   ├── slo.py                   SLI → SLO → error budget
│   ├── sentry_filters.py        drop probe noise, keep probe failures
│   └── tests/                   8 test modules
├── tasks/                       the only business model
│   ├── models.py  serializers.py  views.py  urls.py  admin.py
│   ├── management/commands/seed_tasks.py
│   └── tests/test_api.py
├── monitoring/
│   ├── prometheus/              scrape config + generated alert rules
│   │   ├── prometheus.yml           compose target (web:8000)
│   │   └── prometheus.local.yml     native target (127.0.0.1:8000)
│   └── grafana/                 provisioning + 2 dashboards
├── scripts/                     bootstrap, smoke test, drills, loadgen
├── docs/
│   ├── SLO.md                   objectives, SLIs, error budget, policy
│   ├── architecture.md          15 design decisions and what was not built
│   ├── KNOWN_ISSUES.md          15 known limitations
│   ├── incident-drills.md       measured drill results, including the gaps
│   ├── native-monitoring.md     running Prometheus + Grafana without Docker
│   ├── post-incident-template.md
│   ├── DEMO.md                  five-minute walkthrough
│   └── runbooks/                7 runbooks
├── Dockerfile                   multi-stage, non-root, healthcheck
├── docker-compose.yml           web · db · prometheus · grafana · loadgen
└── .github/workflows/ci.yml     lint, tests, drift checks, image build
```

---

## Configuration

Everything is environment-driven, so the same image runs locally, in CI and in
production. [`.env.example`](.env.example) documents every variable.

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | — | **Required.** The app refuses to start without it when `DEBUG=False` |
| `DEBUG` | `False` | |
| `DATABASE_URL` | `sqlite:///db.sqlite3` | PostgreSQL/Neon in production |
| `SENTRY_DSN` | empty | Sentry is optional, not required |
| `SENTRY_SEND_PII` | `False` | Request bodies and headers carry credentials |
| `PROMETHEUS_URL` | empty | Empty → the SLO reporter uses the in-process registry |
| `SLO_AVAILABILITY_TARGET` | `0.995` | |
| `SLO_LATENCY_TARGET_MS` | `300` | |
| `LOG_FORMAT` | `json` | `console` for a readable terminal |
| `LOG_TO_FILE` | `True` | Set `false` in containers; stdout is collected there |
| `ENABLE_FAULT_ENDPOINTS` | `False` | **Must stay false in production** |

---

## What is verified, and what is not

Being precise about this matters more than the claim itself. "Tested" covers
several different things, and conflating them is how a project ends up confident
and wrong.

| Verified | How |
|---|---|
| The application's behaviour and its operational guarantees | 136 tests, `ruff`, migration and rule-drift checks |
| The deployed HTTP surface — ports, environment, migrations, every endpoint | `scripts/smoke_test.py`, 11 checks against a running instance |
| Detection: failures are captured, counted once, correlated and alerted on | `scripts/run_drill.py` |
| **Prometheus really scrapes the application** | native run, both targets `up` at a 15 s interval |
| **Every alert expression is valid against real data** | all 9 rules report `health=ok`; `promtool check rules` passes |
| **Alerts fire when the SLO is breached, and not before** | `AvailabilityBudgetBurnFast` and `HighErrorRate` observed firing; the rest correctly inactive |
| **Every dashboard panel renders** | 27 of 27 panel queries executed through Grafana returned data |
| **Readiness fails closed while liveness survives a database outage** | end-to-end: `/readyz/` 503 in exactly `DB_CONNECT_TIMEOUT`, `/healthz/` 200 throughout |

| **Not** verified here | Covered by | See |
|---|---|---|
| The container image and compose wiring | CI: `docker-build` and `end-to-end` jobs | — |
| Alert *delivery* — nothing is notified | nothing; this is a real gap | [KNOWN_ISSUES #8](docs/KNOWN_ISSUES.md) |
| Sentry receiving a real event | nothing; no DSN is configured | [KNOWN_ISSUES #12](docs/KNOWN_ISSUES.md) |
| Anything behind a load balancer (TLS, proxy buffering, real client latency) | nothing; this is a demo service | — |
| Multi-worker metrics | nothing; deliberately one worker | [KNOWN_ISSUES #1](docs/KNOWN_ISSUES.md) |

Full detail, including the numbers and the environment each run used:
[`docs/incident-drills.md`](docs/incident-drills.md).

---

## Honest limitations

The full list is in [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md). The headline
ones:

- **No Alertmanager.** Alerts fire and are visible at `/alerts`, and are not
  delivered anywhere. The rules already carry `severity` labels, so adding
  delivery is configuration rather than rewriting.
- **One gunicorn worker.** With more, each worker has its own metrics registry and
  Prometheus would scrape a random worker's partial counters. Multiprocess mode
  fixes it but changes what gauges mean — see the issue entry.
- **No disk-space alert.** That needs `node_exporter`, which is more machinery than
  a single-container project justifies. The manual check is in a runbook.
- **No backup restore drill.** "We have backups" and "we have restored from a
  backup" are different claims, and only the second is evidence.
- **No per-endpoint SLOs.** The objective is service-wide, so a broken
  `/api/tasks/stats/` is diluted by healthy traffic elsewhere.

---

## License

MIT — see [LICENSE](LICENSE).

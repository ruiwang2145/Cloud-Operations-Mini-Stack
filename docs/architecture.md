# Architecture and design decisions

Why the service is built the way it is. [`README.md`](../README.md) describes what
it does; this file explains the choices, including the ones that were close calls.

## The shape of the thing

```
                      ┌──────────────────┐
                      │   UptimeRobot    │  external, every 5 min
                      └────────┬─────────┘
                               │ GET /healthz/
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  web  ·  gunicorn + Django 5.2                               │
│                                                              │
│   ┌────────────────────────────────────────────────────────┐ │
│   │ middleware (outermost first)                           │ │
│   │   PrometheusBeforeMiddleware    framework timer        │ │
│   │   RequestIdMiddleware           correlation id         │ │
│   │   ObservabilityMiddleware       access log + RED       │ │
│   │   Security / sessions / csrf / auth                    │ │
│   │   PrometheusAfterMiddleware     framework timer        │ │
│   └────────────────────────────────────────────────────────┘ │
│                                                              │
│   ops/     probes · metrics · logging · SLO reporter         │
│   tasks/   the only business model, exposed over DRF         │
└───────────┬──────────────────────────┬───────────────────────┘
            │                          │
   /metrics │ scrape 15s               │ SQL
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

The application is deliberately small. Everything interesting is in how it is
operated, and a larger business domain would only obscure that.

---

## Decision 1 — a separate `ops` app, not scattered code

**Decision.** Health probes, metrics, structured logging, the SLO reporter and the
fault-injection endpoints all live in `ops/`.

**Why.** "The operational surface is separable from the product surface" is easy
to claim and hard to verify. Putting it in one app makes it verifiable: you can
read `ops/` top to bottom and know exactly what the service exposes to an operator.
It also means the metrics a dashboard depends on are defined in one file, which is
what makes the drift checks in `ops/tests/test_dashboards.py` possible at all.

**Cost.** An extra import boundary, and a settings module that reaches into an app
for `ops.sentry_filters.drop_probe_events`.

---

## Decision 2 — liveness and readiness are separate endpoints

**Decision.** `/healthz/` does no I/O. `/readyz/` checks the database and returns
503 when it is unusable.

**Why.** They have opposite failure behaviours:

| Probe | Failing means | Correct response |
|---|---|---|
| Liveness | The process is wedged | Restart it |
| Readiness | A dependency is unavailable | Stop sending traffic, do not kill it |

If liveness also checked the database, a database outage would make the
orchestrator restart every healthy worker in a loop — turning someone else's
outage into a self-inflicted denial of service. `ops/tests/test_probes.py` has a
test that fails if a database call is ever added to the liveness view, because
that is an easy and well-intentioned mistake.

**Consequence.** A liveness probe that only returns 200 can look useless. It is
not: it answers "should this process be restarted?", and the answer is usually no.

---

## Decision 3 — one middleware records metrics *and* the access log

**Decision.** `ObservabilityMiddleware` does both, from a single timer. Request-id
correlation is a separate middleware.

**Why.** Two middleware measuring the same request would drift: the log would say
41 ms and the histogram would record 43 ms, and the first person to notice would
have to work out which one to trust. One timer, one truth.

Correlation stays separate because it has a different lifecycle — it has to be set
before anything else can log, and it must be reset even if the request fails.

**Cost.** A middleware with two responsibilities. The alternative is worse.

---

## Decision 4 — errors are observed through `process_exception`, not `try/except`

**Decision.** `ObservabilityMiddleware.process_exception(request, exception)` is
where unhandled exceptions are counted and logged.

**Why.** Django wraps **every** middleware in `convert_exception_to_response`
while building the handler chain (`django/core/handlers/base.py`). The practical
effect is that an exception raised in a view never crosses a middleware boundary:
by the time control returns to a middleware, Django has already turned it into a
500 response.

A naive `try/except` around `self.get_response(request)` therefore never fires —
and the requests that crashed would be the only ones missing from the error
metrics. The exception hook is the only place that sees the raw exception.

**The double-counting problem.** Both observers exist, and both would count the
same failure. The flag goes on the *request*, not the response, because the two
observers hold different response objects — Django builds a brand new 500 after
`process_exception` returns. The request object is the same instance throughout.
`scripts/run_drill.py` asserts the counter rises by exactly 1.

---

## Decision 5 — metrics are labelled by URL pattern, never by path

**Decision.** `ops/endpoints.py::normalise_endpoint` returns the *resolved route*
(`/api/tasks/{pk}/`), or the literal string `unmatched`.

**Why.** This is the single most common way a Prometheus deployment falls over. If
the raw path is a label value, `/api/tasks/1/`, `/api/tasks/2/` and
`/api/tasks/3/` are three time series. With a few thousand tasks the process runs
out of memory building series nobody will ever query, and Prometheus does not stop
you — it just gets slower until it does not work any more.

Unresolved requests all collapse into `unmatched`, which is also exactly the signal
you want when a scanner is walking the site. The scan drill
(`scripts/run_drill.py --drill unknown-path-scan`) asserts that 200 distinct URLs
produce one series.

**Detail worth knowing.** DRF's router builds routes as regular expressions, so a
detail endpoint resolves to `^tasks/(?P<pk>[^/.]+)/$`. `normalise_endpoint`
rewrites that into `{pk}` form, because both are bounded but only one is readable
on a Grafana legend.

---

## Decision 6 — probe traffic is excluded from every SLI

**Decision.** `/healthz/`, `/readyz/`, `/metrics` and `/version/` are excluded from
the availability and latency indicators, in the application, the alert rules and
the dashboards.

**Why.** Those requests are generated by monitoring on a fixed schedule. Counting
them would let a probe loop that never fails drag the measured error rate towards
zero no matter what real users experienced — the monitoring system flattering
itself.

The exclusion is applied in three places that must agree, so it is asserted:
`ops/tests/test_dashboards.py` checks the dashboards,
`ops/tests/test_alert_rules.py` checks the rules, and
`ops/tests/test_probes.py` checks the SLI itself.

---

## Decision 7 — the SLO lives in code and the alerts are generated from it

**Decision.** Objectives are defined once, in `settings.py`.
`scripts/render_rules.py` renders `alert_rules.yml` from them, and
`ops/tests/test_alert_rules.py` fails the build if the committed file drifts.

**Why.** The objective appears in three places: the API report, the documentation,
and the alert rules. The third is the one that matters at 03:00 and the one most
likely to be stale — someone edits the objective, the API and dashboard report the
new number, and the alert keeps firing against the old one. Nobody notices until an
incident where the alert stays silent.

Generating the rules removes the possibility rather than relying on discipline.

**Cost.** A generated file in the repository, and a test that fails confusingly if
someone sets an `SLO_*` environment variable before running the suite.

---

## Decision 8 — 4xx is not an error

**Decision.** Availability is `1 - (5xx / total)`. A rejected request does not count
against the objective.

**Why.** A 403 from an unauthenticated write, or a 400 from a malformed payload, is
the service doing exactly what it should. Counting it would make the SLI a measure
of who is calling rather than of whether the service works — and it would punish
the service for having a public API.

**Cost.** A bug that returns 400 instead of 500 is invisible to the SLO. That is a
real gap, and it is recorded in [KNOWN_ISSUES.md](KNOWN_ISSUES.md) rather than
papered over.

---

## Decision 9 — a latency compliance ratio, measured at a bucket boundary

**Decision.** The latency SLI is
`requests faster than 300 ms / total`, and `0.3` is an explicit histogram bucket
boundary in `ops/metrics.py`.

**Why.** `histogram_quantile` interpolates within a bucket, so it is accurate in
the middle and approximate at the edges. Since the objective is 300 ms, making
300 ms an actual bucket boundary turns the SLI into an exact lookup
(`le="0.3"`) rather than an estimate. The bucket list below 300 ms exists to make
the distribution legible in Grafana.

If the objective is changed to a value that is not a bucket, the SLI is measured at
the next boundary up and `/api/slo/` reports which one was used
(`latency.bucket_ms`). Silently measuring against a threshold nobody chose is worse
than being slightly conservative.

---

## Decision 10 — a hand-written JSON log formatter

**Decision.** `ops/logging.py` contains a `JsonFormatter` rather than using
`python-json-logger`.

**Why.** The original implementation used an f-string formatter —
`'{"msg":"%(message)s"}'` — which produces invalid JSON the first time a message
contains a quote or a newline, which is exactly when the log matters most. It also
cannot carry extra fields.

A formatter built on `logging.Formatter` fixes both and keeps the schema under our
control. It also removes a dependency whose import path changed between major
versions, which is a class of breakage that has nothing to do with this project.

**The schema is fixed for the fields that always exist** (`ts`, `level`, `logger`,
`message`, `service`, `version`, `request_id`) and open for anything passed via
`extra=`. `message` is an *event name* (`http_request`), not a sentence, because an
event name can be counted and a sentence cannot.

---

## Decision 11 — a correlation id from a context variable

**Decision.** `X-Request-ID` is read from the inbound request (or generated), held
in a `contextvars.ContextVar`, and echoed on the response.

**Why.** It is what turns "the user says it failed at 14:02" into a single log
query. A context variable rather than threading a parameter through every function
signature: code deep in the call stack can read it without every caller knowing it
exists.

A filter, not a call site, attaches it to each record — correlation has to be
impossible to forget. Log lines emitted from a library, a signal handler or an
exception handler still get the id.

**The filter approach is what makes it work for logs the application did not
write.**

---

## Decision 12 — the SLO reporter degrades instead of failing

**Decision.** When Prometheus is unreachable, `/api/slo/` falls back to the
in-process registry and reports `"window_source": "process_lifetime"`.

**Why.** A monitoring endpoint that returns 500 because monitoring is down is
useless precisely when it is needed. Degrading and *saying so* is better than
failing and better than lying.

**The fallback is not equivalent** and the response makes the difference explicit.
`SloReporterDegraded` fires whenever it is used, so the degradation is visible
rather than silent.

---

## Decision 13 — one gunicorn worker by default

**Decision.** The container runs `--workers 1`.

**Why.** `prometheus_client` keeps counters in process memory. With N workers
there are N registries, Prometheus scrapes whichever worker answers, and the
dashboards show a fraction of real traffic changing unpredictably between scrapes.
Metrics that look fine and are wrong are worse than no metrics.

The fix is multiprocess mode, and it is *not* a drop-in change: gauges are
aggregated across processes by summation, so `app_slo_availability_ratio` from
three workers would report `3.0`. See
[KNOWN_ISSUES #1](KNOWN_ISSUES.md#1-metrics-are-per-worker-under-gunicorn).

**This is a correctness-over-throughput trade-off, made deliberately and written
down.**

---

## Decision 14 — fault-injection endpoints default to off, and 404 when disabled

**Decision.** `/boom/` and `/sentry-debug/` require `ENABLE_FAULT_ENDPOINTS=true`
and return **404** (not 403) when disabled.

**Why.** 404 rather than 403: a 403 confirms the endpoint exists and invites
someone to keep trying. A 404 says nothing at all.

A test reads `settings.py` and fails if the default is ever flipped, because the
safe value has to be the default rather than a line in `.env.example`. Reading the
documentation is not a control; the code is.

---

## Decision 15 — configuration is entirely environment-driven

**Decision.** One image runs everywhere; only the injected variables change.
`.env` is a local convenience and a no-op in a container.

**Why.** "The image that runs in production is not the image that passed CI" is a
class of bug that disappears when there is only one image.

`SECRET_KEY` is the exception that proves the rule: the application **refuses to
start** without one when `DEBUG` is false. A service that boots with a guessable
signing key is worse than a service that does not boot, because nobody notices.
`ALLOW_INSECURE_SECRET_KEY` exists for CI and for the alert-rule generator, and its
name says exactly what it does.

---

## Deliberate non-decisions

Things that are *not* here, and why. Each one is a reasonable next step rather than
an oversight.

| Not built | Why not | What it would take |
|---|---|---|
| **Alertmanager** | The alerts and their `severity` labels are already correct; delivery is deployment configuration | A routing tree. Alerts are visible at `/alerts` today |
| **Redis cache** | `LocMemCache` is fine for a read-only 15-second cache with one worker | A shared cache; see [KNOWN_ISSUES #6](KNOWN_ISSUES.md) |
| **`node_exporter`** | Adds a second daemon to a single-container project | Host metrics and a disk-space alert; see [KNOWN_ISSUES #9](KNOWN_ISSUES.md) |
| **Distributed tracing** | Sentry APM covers the single-service case; a trace with one span is not a trace | OpenTelemetry, a collector, and a second service to trace across |
| **Per-endpoint SLOs** | Two objectives nobody has tuned are worse than one that is understood | A recording rule per endpoint, and a reason to care |
| **`django-filter`** | Two filters, handled explicitly in `get_queryset` | A dependency, and a second place where query semantics live |
| **Kubernetes manifests** | Compose is the honest scale for this service; manifests would be theatre | Real manifests, and a cluster to run them on |

---

## Where to look next

| Question | File |
|---|---|
| What are the objectives and the budget policy? | [`docs/SLO.md`](SLO.md) |
| What is broken or accepted? | [`docs/KNOWN_ISSUES.md`](KNOWN_ISSUES.md) |
| Something is wrong right now | [`docs/runbooks/`](runbooks/) |
| Does detection actually work? | [`docs/incident-drills.md`](incident-drills.md) |
| How do I demo this? | [`docs/DEMO.md`](DEMO.md) |

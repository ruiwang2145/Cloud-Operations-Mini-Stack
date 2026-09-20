# Running the monitoring stack without Docker

The full stack normally runs under `docker compose`. This is the alternative for
when Docker is not available — no administrator rights, no reboot, no container
runtime, and about 600 MB of downloads.

## Why bother

Running Prometheus and Grafana natively is not a substitute for the containerised
deployment; it verifies a different set of things, and those things are the ones
that are easy to get wrong and impossible to see in a config file:

| Question | How it is answered |
|---|---|
| Does the application export the metric names the rules reference? | Prometheus scrapes and the rules evaluate |
| Is every alert expression valid against real data? | `promtool check rules` plus a live `/api/v1/rules` |
| Do the dashboards render, or are the panels empty? | Every panel query executed through Grafana |
| Does the readiness probe really take the instance out of rotation? | The readiness drill in [incident-drills.md](incident-drills.md) |

All four are covered by the drill log, which records the results this setup
produced. A container build verifies none of them.

## What it does not verify

- The `Dockerfile` and the compose wiring (image build, entrypoint, healthcheck,
  service discovery, the `web:8000` target name).
- Anything about a real deployment: TLS, a load balancer, secret injection.

Both are checked in CI by the `docker-build` and `end-to-end` jobs.

## Prerequisites

- Python 3.10+ with the project's dependencies installed
  (`scripts/bootstrap.sh` or `scripts/bootstrap.ps1`)
- ~600 MB of free disk for the two binaries
- Nothing else. No Docker, no WSL, no administrator rights

---

## Step 1 — Start the application

```bash
# from the repository root
.venv/bin/python manage.py runserver 127.0.0.1:8000      # Windows: .venv\Scripts\python.exe
```

Confirm it is serving and exporting metrics:

```bash
curl -sS http://127.0.0.1:8000/healthz/
curl -sS http://127.0.0.1:8000/metrics | head -20
```

## Step 2 — Get Prometheus

Download the Windows/macOS/Linux build from
<https://github.com/prometheus/prometheus/releases> and unpack it somewhere
outside the repository (the binaries are 100+ MB and must not be committed).

```bash
# example, Windows
curl -L -o prometheus.zip \
  https://github.com/prometheus/prometheus/releases/download/v3.14.0/prometheus-3.14.0.windows-amd64.zip
```

## Step 3 — Check the config before running it

```bash
# both of these should say SUCCESS
promtool check config  monitoring/prometheus/prometheus.local.yml
promtool check rules   monitoring/prometheus/alert_rules.yml
```

This is worth doing on its own: a malformed rule file makes Prometheus refuse to
start, which is a spectacular way to lose monitoring while changing monitoring.

## Step 4 — Start Prometheus

```bash
prometheus \
  --config.file=monitoring/prometheus/prometheus.local.yml \
  --storage.tsdb.path=/tmp/prometheus-data \
  --storage.tsdb.retention.time=35d \
  --web.listen-address=127.0.0.1:9090 \
  --web.enable-lifecycle
```

`prometheus.local.yml` targets `127.0.0.1:8000` and loads the same
`alert_rules.yml` the container uses — the only difference from the committed
`prometheus.yml` is the target hostname, because `web:8000` only resolves inside
the compose network.

Verify:

```bash
curl -sS http://127.0.0.1:9090/-/healthy
curl -sS http://127.0.0.1:9090/api/v1/targets    # both targets should be "up"
curl -sS http://127.0.0.1:9090/api/v1/rules     # 9 rules across 3 groups
```

Open <http://127.0.0.1:9090/alerts>.

## Step 5 — Get and start Grafana

Download the standalone build from <https://grafana.com/grafana/download> and
unpack it outside the repository.

Grafana's provisioning files point at the repository's dashboards, and
`options.path` in the dashboards provider must be an **absolute** path — Grafana
resolves it relative to its own working directory, not to the config file. Create
a small provisioning directory rather than editing the committed one:

```
<somewhere>/provisioning/
├── datasources/prometheus.yml     url: http://127.0.0.1:9090
└── dashboards/dashboards.yml      path: <absolute path to the repo>/monitoring/grafana/dashboards
```

Copy `monitoring/grafana/provisioning/datasources/prometheus.yml` and change the
`url` to `http://127.0.0.1:9090`, then copy
`monitoring/grafana/provisioning/dashboards/dashboards.yml` and set `options.path`
to the absolute path of `monitoring/grafana/dashboards`.

Then:

```bash
GF_PATHS_PROVISIONING=<somewhere>/provisioning \
GF_PATHS_DATA=<somewhere>/data \
GF_AUTH_ANONYMOUS_ENABLED=true \
GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
GF_SECURITY_ADMIN_PASSWORD=admin \
grafana server --homepath <grafana install dir>
```

Open <http://127.0.0.1:3000>. Both dashboards appear under the **Cloud Ops**
folder.

## Step 6 — Generate traffic

Empty dashboards prove nothing. The load generator produces a mix of successful
reads, 4xx and 5xx traffic on purpose:

```bash
TARGET_URL=http://127.0.0.1:8000 LOADGEN_RPS=6 LOADGEN_ERROR_RATIO=0.01 \
  .venv/bin/python scripts/loadgen.py
```

Leave it running for a few minutes, then check the dashboards.

---

## Drilling the readiness failure

The interesting drill needs the service to be **up** while its database is
**down**. `manage.py runserver` cannot do that — it calls `check_migrations()`
during startup, which needs a connection, so the process exits before it can ever
answer `/readyz/`.

gunicorn does no such check, which is exactly why a production container *can* be
up-but-not-ready. `scripts/serve_for_drills.py` reproduces that behaviour with the
standard library alone:

```bash
DATABASE_URL=postgresql://bad:bad@127.0.0.1:59999/bad \
  .venv/bin/python scripts/serve_for_drills.py
```

Then:

```bash
# Must stay 200 throughout -- if this fails, the orchestrator restarts every
# healthy worker during a database outage.
curl -o /dev/null -w 'healthz %{http_code}\n' http://127.0.0.1:8000/healthz/

# Must be 503, naming the dependency that is down.
curl -sS http://127.0.0.1:8000/readyz/

# The counter must rise.
curl -sS http://127.0.0.1:8000/metrics | grep '^app_readiness_failures_total'
```

Results are recorded in [incident-drills.md](incident-drills.md).

---

## Cleanup

Both processes are foreground; stop them with Ctrl-C. Delete the Prometheus data
directory and the Grafana data directory when finished — Prometheus will hold
roughly 35 days of history if you leave it running, and the TSDB is not small.

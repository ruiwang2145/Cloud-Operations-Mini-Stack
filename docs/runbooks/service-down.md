# Runbook: service is down

**Alerts:** `ServiceDown` (critical), `NoTraffic` (warning)

## Symptoms

- `ServiceDown`: Prometheus has failed to scrape the service for 2 minutes
  (`up{job="cloud-ops-mini-stack"} == 0`).
- `NoTraffic`: the service is being scraped successfully but has served no
  application traffic for 30 minutes.
- Users report connection refused, timeouts, or a 502/503 from whatever sits in
  front of the service.

These are two different failures that look similar from the outside:

| Alert | Meaning |
|---|---|
| `ServiceDown` | The process is gone, or `/metrics` is not answering |
| `NoTraffic` | The process is fine but nobody is reaching it — routing, DNS or a load balancer |

Diagnosing the second as if it were the first wastes the most valuable minutes.

## Impact

Complete loss of service if `ServiceDown`. If only `NoTraffic`, requests are
either not arriving at all or failing before they get here.

## First five minutes

```bash
# 1. Is the process answering anything at all?
curl -sS -m 5 http://localhost:8000/healthz/

# 2. What does Prometheus think, and for how long?
#    http://localhost:9090/graph?g0.expr=up%7Bjob%3D%22cloud-ops-mini-stack%22%7D
curl -sS -m 5 http://localhost:9090/api/v1/query \
  --data-urlencode 'query=up{job="cloud-ops-mini-stack"}' | python -m json.tool

# 3. Is the container running, and is it restarting in a loop?
docker compose ps
docker compose logs --tail=100 web
```

If step 1 answers, the problem is between the caller and the service (proxy, DNS,
load balancer, security group) — skip to **Diagnose: reachability**.

If step 1 fails, the process is the problem — continue below.

## Diagnose

### Is the process alive?

```bash
docker compose ps web
# Look at STATUS. "Restarting" is a crash loop: the entrypoint is failing before
# gunicorn starts, and the logs from the *previous* attempt are the useful ones.
docker compose logs --tail=200 web
```

Common causes, in the order they actually occur:

1. **Missing or invalid configuration.** The application refuses to start without
   `SECRET_KEY` (see `my_cloudapp/settings.py`), and a malformed `DATABASE_URL`
   fails during `migrate` in the entrypoint.
   ```bash
   docker compose logs web | grep -iE "error|traceback|improperlyconfigured"
   ```
2. **Migrations failed.** The entrypoint runs `migrate` before starting gunicorn;
   a failed migration exits non-zero and the container never comes up. The
   message names the migration.
3. **The database is unreachable.** The entrypoint waits up to `DB_WAIT_TIMEOUT`
   (default 60 s) and then exits. This looks like "the app is down" but is really
   [database-unreachable.md](database-unreachable.md).
4. **Port already in use.** A previous container or a local `runserver` is holding
   8000. `docker compose logs web` says so explicitly.

### Is `/metrics` the only thing failing?

```bash
curl -sS -m 5 http://localhost:8000/metrics | head -5
```

If `/healthz/` works but `/metrics` does not, the process is fine and Prometheus
is being lied to. Note the deliberate absence of a trailing slash: `/metrics/`
returns 404, and a scrape config pointing at `/metrics/` produces exactly this
symptom while looking correct. Check `monitoring/prometheus/prometheus.yml`.

### Is the service reachable but nothing is calling it?

`NoTraffic` firing means scrapes succeed. So the question is upstream:

```bash
# Does the service see any traffic at all? Probe traffic is excluded from the
# alert, so check the raw counter instead.
curl -sS http://localhost:8000/metrics | grep '^app_http_requests_total'

# Is anything arriving from outside? Run a request from another host.
curl -sS -m 5 -o /dev/null -w '%{http_code}\n' http://<host>:8000/healthz/
```

Check, in order: the load balancer's target health (is the instance registered?),
`ALLOWED_HOSTS` (Django returns 400 for a host it does not know, which the load
balancer may read as unhealthy), and DNS.

## Mitigate

Do the fastest thing that restores service, then investigate.

```bash
# Restart in place. This is the right first move for a wedged process and costs
# nothing if it turns out to be something else.
docker compose restart web

# If it is a bad deploy, roll back rather than debugging forward:
# see deploy-and-rollback.md
```

If the restart fixes it and you do not know why, that is not a resolution — it is
a postponement. Capture the state before the container is replaced:

```bash
docker compose logs --tail=1000 web > /tmp/incident-$(date +%s).log
curl -sS http://localhost:8000/metrics > /tmp/incident-metrics-$(date +%s).txt
```

## Verify

All four must be true. Anything less is "it looks better".

```bash
curl -sS -o /dev/null -w 'liveness  %{http_code}\n' http://localhost:8000/healthz/
curl -sS -o /dev/null -w 'readiness %{http_code}\n' http://localhost:8000/readyz/
python scripts/smoke_test.py
```

- [ ] `/healthz/` returns 200
- [ ] `/readyz/` returns 200 (not 503 — that would mean it is up but not usable)
- [ ] `up{job="cloud-ops-mini-stack"}` is 1 again in Prometheus
- [ ] The smoke test passes all 11 checks

The `ServiceDown` alert clears 2 minutes after scrapes resume. Do not close the
incident before it does — a service that is up but not *scrapable* will page again.

## Follow-up

- What was the actual root cause? "It restarted and worked" is not one.
- Was detection fast enough? The `for: 2m` on `ServiceDown` means the fastest
  possible page is ~2 minutes after the last successful scrape. If that was too
  slow, change the rule (and the test that pins the alert set).
- Did the runbook get you to the answer, or did you work it out yourself? If the
  latter, add what you learned here — that is the whole mechanism by which a
  runbook becomes useful.

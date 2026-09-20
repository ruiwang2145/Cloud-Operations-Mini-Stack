# Runbook: the database is unreachable

**Alert:** `ReadinessCheckFailing` (warning)

## Symptoms

```bash
curl -sS http://localhost:8000/readyz/
```

```json
{
  "status": "not_ready",
  "checks": {
    "database": {"ok": false, "error": "OperationalError: could not connect to server", "latency_ms": 5003.12}
  }
}
```

`ReadinessCheckFailing` fires when `app_readiness_failures_total` rises.

**Note what has *not* happened: the service is still running.** A failing
readiness probe takes the instance out of the load balancer without killing it.
That is deliberate — see below — and it is the reason this runbook is a warning
and not a page.

## Impact

Users see errors only if *every* instance is unready. One unready instance out of
three means the load balancer routes around it and nobody notices — which is
exactly what readiness is for.

The dangerous version of this incident is the one where liveness also fails, so
the orchestrator restarts every worker in a loop. That turns a database outage
into a self-inflicted denial of service. **Liveness deliberately does not touch
the database**, and `ops/tests/test_probes.py` has a test that fails if anyone
ever adds a database call to `/healthz/`.

## First five minutes

```bash
# 1. What exactly is failing, and how long did it take to fail?
curl -sS http://localhost:8000/readyz/ | python -m json.tool

# 2. Is liveness still fine? It must be. If it is not, this is service-down.md.
curl -sS -o /dev/null -w 'liveness %{http_code}\n' http://localhost:8000/healthz/

# 3. Is the database process even up?
docker compose ps db
docker compose logs --tail=100 db

# 4. Can the database accept a connection from inside its own container?
docker compose exec db pg_isready -U cloudops -d cloudops
```

Note the `latency_ms` in step 1. A failure that takes 5 seconds is a **timeout**
(a network path or a full connection pool); a failure that takes 2 milliseconds is
a **refusal** (nothing is listening). They have different causes and different
fixes, and the response body tells you which one you have.

| `latency_ms` | Meaning | Look at |
|---|---|---|
| ~0–10 ms | Connection refused — nothing listening | Is the database process running? Is the port right? |
| ~5000 ms (your `DB_CONNECT_TIMEOUT`) | Timeout — something is listening but not answering | Network path, host firewall, connection pool exhaustion |
| ~1000–2000 ms | DNS resolution slow or failing | `DATABASE_URL` hostname, DNS |

## Diagnose

### 1. Is it DNS?

This is the most common cause with a hosted database (Neon, RDS) and the one that
looks least like a database problem.

```bash
docker compose exec web getent hosts <db-hostname>
# or, inside a python shell:
docker compose exec web python -c "import socket; print(socket.getaddrinfo('<db-host>', 5432))"
```

A DNS failure inside the container but not on the host means the container's
resolver is misconfigured, not that the database is down.

### 2. Is it the connection string?

```bash
docker compose exec web python - <<'PY'
import os
from urllib.parse import urlsplit
url = urlsplit(os.environ["DATABASE_URL"])
print("scheme:", url.scheme)
print("host:  ", url.hostname)
print("port:  ", url.port)
print("db:    ", url.path.lstrip("/"))
print("user:  ", url.username)
print("ssl:   ", "sslmode" in (url.query or ""))
PY
```

The password is deliberately not printed — logs and screen shares are how
credentials leak. If you need to check it, check its length.

### 3. Are connections exhausted?

```bash
docker compose exec db psql -U cloudops -d cloudops -c "
  SELECT count(*) AS total,
         count(*) FILTER (WHERE state = 'active') AS active,
         count(*) FILTER (WHERE state = 'idle')   AS idle
  FROM pg_stat_activity WHERE datname = 'cloudops';"
```

If `total` is at the server's `max_connections`, every new request fails while
existing ones keep working — so the service looks intermittently broken rather
than down. See `DB_CONN_MAX_AGE` in `settings.py`: persistent connections reduce
the churn that causes this, at the cost of holding connections open.

### 4. Did the credentials rotate?

A rotated password that was not propagated to the service produces an
authentication error, not a connection error. The `error` field in `/readyz/`
says which:

```
OperationalError: FATAL:  password authentication failed for user "cloudops"
```

## Mitigate

1. **If only some instances are affected**, do nothing yet — they have already
   removed themselves from rotation. Fix the cause.
2. **If the database process died**, restart it:
   ```bash
   docker compose restart db
   ```
   Persistent volumes mean the data survives; confirm that before assuming it.
3. **If connections are exhausted**, recycle the idle ones:
   ```bash
   docker compose exec db psql -U cloudops -d cloudops -c "
     SELECT pg_terminate_backend(pid) FROM pg_stat_activity
     WHERE datname = 'cloudops' AND state = 'idle'
       AND state_change < now() - interval '10 minutes';"
   ```
   This buys time; it does not fix the leak. Find what is holding connections.
4. **If it is a managed database outage**, there is nothing to fix locally. The
   service is behaving correctly by staying up and unready. Verify the provider's
   status page, and confirm that `up{job="cloud-ops-mini-stack"}` is still 1 — if
   it is, the monitoring is working exactly as designed during someone else's
   outage.

## Verify

```bash
curl -sS http://localhost:8000/readyz/ | python -m json.tool
# Expect: status "ready", database.ok true, latency_ms in single digits
```

- [ ] `/readyz/` returns 200
- [ ] `latency_ms` is in single digits — a 200 that takes 4 seconds means the
      dependency is up but degraded, and the next request will fail
- [ ] `increase(app_readiness_failures_total[5m])` has stopped rising
- [ ] `/healthz/` returned 200 throughout — if it did not, the orchestrator may
      have restarted workers and you have a second problem

## Follow-up

- How long was the instance out of rotation, and did any user notice? If a user
  noticed, the readiness probe is not buying what it should: either there are too
  few instances, or readiness is not actually wired into the load balancer.
- If the cause was a credential rotation, write down the order the rotation has to
  happen in, and put it in the deploy runbook.
- If the cause was connection exhaustion, that is a leak. Find it before the next
  occurrence.

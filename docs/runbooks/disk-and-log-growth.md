# Runbook: disk and log growth

**Alerts:** none — this is the runbook for the gap in the alerting. See the note at
the end.

## Symptoms

- A write fails with "no space left on device".
- PostgreSQL refuses writes, or the container exits.
- Prometheus stops ingesting and its UI shows gaps.
- The service is slow or crash-looping with no obvious application error.

This is the least glamorous failure mode and one of the most common, because
nothing in the application logs says "the disk is full" until something already
failed.

## Why it is not alerted on

Prometheus can alert on disk space with `node_exporter`, which this stack does not
run — it would add a daemon set's worth of machinery to a single-container demo.
That is a real gap, and it is documented here rather than papered over: **the
first time you deploy this to a real host, add `node_exporter` and an alert on
`node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.15`.**

## First five minutes

```bash
# 1. Docker's own view of everything it owns.
docker system df

# 2. Where is the space going?
docker system df -v

# 3. The volumes this stack creates.
docker volume ls | grep cloud-ops
```

## Diagnose

### Which volume?

| Volume | Grows because | Bounded? |
|---|---|---|
| `cloud-ops-mini-stack_prometheus-data` | Scrapes accumulate | Yes — `--storage.tsdb.retention.time=35d` |
| `cloud-ops-mini-stack_postgres-data` | Application data, WAL | No |
| `cloud-ops-mini-stack_grafana-data` | Dashboards and users | Effectively yes |
| Container writable layers | Log files written *inside* the container | **Should be zero** |

The last row is the one to check first, because it is the one that should never
happen.

```bash
# Anything written inside the container is invisible to `docker system df` until
# the container is removed. This is where a log file ends up if LOG_TO_FILE is on.
docker compose exec web sh -c 'ls -la /app/*.log 2>/dev/null; du -sh /app 2>/dev/null'
```

### Logs

`LOG_TO_FILE` defaults to **true** so that local development has a greppable file.
The container image sets it to **false**, because a container's logs belong on
stdout where the log driver owns rotation and retention. If a log file is growing
inside a container, that setting was overridden:

```bash
docker compose exec web sh -c 'env | grep LOG_'
```

Two independent protections are in place for the file handler:

```python
# my_cloudapp/settings.py
"maxBytes": env_int("LOG_MAX_BYTES", 10 * 1024 * 1024),   # 10 MB
"backupCount": env_int("LOG_BACKUP_COUNT", 5),            # 5 files
```

That bounds `app.log` and its rotations at roughly 60 MB. Verify it is working:

```bash
ls -lh app.log*
# Expect at most six files, the largest at 10 MB. More than six means rotation is
# not being applied -- check that the handler is a RotatingFileHandler.
```

### Prometheus

```bash
# How much is it holding?
docker compose exec prometheus du -sh /prometheus

# What is the retention actually set to? A config change that was never reloaded
# looks exactly like retention being ignored.
curl -sS http://localhost:9090/api/v1/status/flags | python -m json.tool | grep -i retention
```

A 35-day retention at a 15-second scrape interval with a handful of targets is
tens of megabytes. If it is gigabytes, either the retention setting is not in
effect or something is creating far more series than expected — which is the
cardinality problem, and worth investigating in its own right:

```bash
curl -sS http://localhost:9090/api/v1/status/tsdb | python -m json.tool | head -40
# Look at seriesCountByMetricName. app_http_requests_total should be tens of
# series, not thousands. If it is thousands, a label is carrying an unbounded
# value -- see ops/endpoints.py.
```

### PostgreSQL

```bash
docker compose exec db psql -U cloudops -d cloudops -c "
  SELECT pg_size_pretty(pg_database_size('cloudops')) AS database,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')) AS wal_written;"

# Table sizes, largest first.
docker compose exec db psql -U cloudops -d cloudops -c "
  SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total
  FROM pg_catalog.pg_statio_user_tables
  ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;"
```

## Mitigate

In order of what loses the least data:

```bash
# 1. Rotate the application log rather than deleting it. Deleting a file that a
#    process holds open does not free the space until the process closes it --
#    the classic "I deleted it and the disk is still full" trap.
docker compose exec web sh -c 'mv /app/app.log /app/app.log.old 2>/dev/null; true'

# 2. Reduce Prometheus retention. This deletes history -- including the history the
#    30-day SLO window is defined over, so the SLO report will start saying
#    "no_data" for a window it can no longer see.
curl -sS -X POST http://localhost:9090/-/reload   # after editing prometheus.yml

# 3. Prune Docker's unused images and build cache. Safe: neither is referenced by
#    a running container.
docker image prune -f
docker builder prune -f
```

**Never** run `docker system prune -a --volumes` on a host holding data you care
about. `--volumes` deletes `postgres-data`, which is the database.

## Verify

```bash
docker system df
docker compose ps
python scripts/smoke_test.py
```

- [ ] Free space is above 15% of the volume
- [ ] Every service is `Up`, not `Restarting`
- [ ] `/readyz/` returns 200 (a full disk often breaks the database first)
- [ ] `ls -lh app.log*` shows at most six files

## Follow-up

- Add the disk alert. This runbook exists because the alert does not, and a
  runbook you have to remember to read is not a control.
- If a log file grew inside a container, fix the override rather than the file.
- If Prometheus grew unexpectedly, treat it as a cardinality incident: find the
  label carrying unbounded values before just deleting the data.

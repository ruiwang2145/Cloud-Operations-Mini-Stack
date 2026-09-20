# Runbook: deploying and rolling back

**Alerts:** none directly. This runbook is the *response* to a deploy that made
things worse — see [high-error-rate.md](high-error-rate.md) and
[service-down.md](service-down.md).

## The deploy

```bash
# 1. Know what you are deploying.
git log --oneline -5
git status --short                 # uncommitted changes do not reach the image
python scripts/render_rules.py --check   # alert rules match the SLO definition?

# 2. Verify before shipping. In this order, cheapest first.
python -m ruff check .
python -m pytest

# 3. Build and start. The image runs migrations and collectstatic in its
#    entrypoint, so this is a complete deploy.
docker compose build web
docker compose up -d web

# 4. Prove it worked -- do not assume.
python scripts/smoke_test.py
curl -sS http://localhost:8000/version/ | python -m json.tool
```

## Why the order matters

**Verify before building.** A build takes minutes and a failing test takes
seconds. Discovering a broken import after the image is built wastes the only
resource you cannot get back during a deploy window.

**Prove it after deploying.** A container that started is not a service that
works. `docker compose ps` saying `Up` only means the process did not exit — the
smoke test is what tells you it is serving correctly. This is the difference
between a deploy and a *verified* deploy.

## The migration question

The entrypoint runs `migrate` on startup. That is fine for one web process and a
race the moment there are two: both run `migrate` concurrently, one wins, the
other crashes with a duplicate-object error.

The production shape is a separate one-shot job:

```bash
# 1. Run migrations once, before rolling out the new version.
docker compose run --rm -e RUN_COLLECTSTATIC=false web \
  python manage.py migrate --noinput

# 2. Then start the new version without re-running them.
#    The switches are exported by the shell and passed through by the `environment:`
#    block in docker-compose.yml. Without that passthrough the command would look
#    like it worked and quietly do the opposite.
RUN_MIGRATIONS=false RUN_COLLECTSTATIC=false docker compose up -d web
```

The switches exist in `scripts/docker-entrypoint.sh` for exactly this. With one
replica the simpler path is correct; with two, it is a bug waiting for a busy
deploy.

**Migrations that cannot be rolled back** are the real hazard. Adding a nullable
column is reversible; dropping one is not. Before deploying a destructive
migration, confirm that the previous version of the code still runs against the
new schema — because that is the state you will be in during a rollback.

## Rollback

Rolling back is a normal operation, not a failure. Decide early: if a deploy
correlates with a new alert, rolling back takes two minutes and debugging forward
takes as long as it takes.

```bash
# 1. What is the previous version?
git log --oneline -10
curl -sS http://localhost:8000/version/ | python -m json.tool

# 2. Roll the code back.
git checkout <previous-sha>
docker compose build web
docker compose up -d web

# 3. Re-verify. A rollback is a deploy and gets the same treatment.
python scripts/smoke_test.py
curl -sS http://localhost:8000/api/slo/ | python -m json.tool
```

### Rolling back a migration

```bash
# Which migration is the current one?
docker compose exec web python manage.py showmigrations tasks

# Step back one migration.
docker compose exec web python manage.py migrate tasks <previous_migration_name>
```

Only do this if the migration is genuinely reversible **and** you have confirmed
that no data written by the new version depends on the new schema. Otherwise you
are trading an availability incident for a data-loss incident, which is a much
worse trade.

If the migration is irreversible, roll *forward* with a fix instead. That is
usually faster than it feels.

## After a rollback

```bash
# Capture the evidence before it is gone.
docker compose logs --tail=2000 web > /tmp/incident-$(date +%s)-web.log
curl -sS http://localhost:8000/metrics > /tmp/incident-$(date +%s)-metrics.txt
```

The container that failed is about to be replaced. If you do not capture its logs
now, the root cause is gone and the incident will repeat.

Then:

- [ ] The alert has cleared (not merely stopped firing — check Prometheus)
- [ ] `/readyz/` returns 200
- [ ] The smoke test passes
- [ ] The failing behaviour is reproduced locally, with a test that fails before
      the fix and passes after it

## Verify checklist

Copy this into the incident notes:

```
Deploy
  [ ] ruff clean
  [ ] pytest green (123 tests)
  [ ] render_rules --check clean
  [ ] image built
  [ ] smoke test: 11/11
  [ ] /version/ reports the expected build
  [ ] up{job="cloud-ops-mini-stack"} == 1
  [ ] no new alerts in the first 10 minutes

Rollback (if needed)
  [ ] previous SHA checked out and built
  [ ] smoke test: 11/11
  [ ] failed container's logs captured
  [ ] alert cleared
  [ ] post-incident review scheduled
```

## Related

- [metrics-and-logs.md](metrics-and-logs.md) — reading what the service did
- [post-incident-template.md](../post-incident-template.md) — writing it up

#!/usr/bin/env sh
# =============================================================================
# Container entrypoint.
#
# Does the three things that must happen before the application starts, and then
# `exec`s the CMD so that the server becomes PID 1 and receives signals directly.
# Without `exec`, Ctrl-C or a `docker stop` would be delivered to this shell and
# gunicorn would be killed instead of shut down gracefully -- in-flight requests
# would be dropped on every deploy.
#
# Every step is switchable by an environment variable, because the same image has
# to serve as the web process, a one-off migration job and a debugging shell.
# =============================================================================
set -eu

log() {
    printf '%s [entrypoint] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# -----------------------------------------------------------------------------
# Prometheus multiprocess mode (opt-in)
#
# Only relevant when running more than one gunicorn worker: with several workers
# each has its own metrics registry, and Prometheus scrapes whichever one answers.
# Setting PROMETHEUS_MULTIPROC_DIR makes prometheus_client share values through
# files on disk; django-prometheus then switches to MultiProcessCollector by
# itself.
#
# The directory MUST start empty. Stale files from a previous container would be
# replayed as if they were current traffic, so a freshly restarted service would
# report the request rate of the run before it -- the worst possible failure mode
# for a monitoring system.
# -----------------------------------------------------------------------------
if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
    log "preparing Prometheus multiprocess directory ${PROMETHEUS_MULTIPROC_DIR}"
    rm -rf "${PROMETHEUS_MULTIPROC_DIR}"
    mkdir -p "${PROMETHEUS_MULTIPROC_DIR}"
fi

# -----------------------------------------------------------------------------
# Wait for the database
#
# Only for PostgreSQL, and only when asked. SQLite needs no handshake, so waiting
# on it would be a pointless sleep that makes local starts feel slow.
#
# This is a convenience for `docker compose up`, not a substitute for a
# orchestrator's own startup ordering: it retries a fixed number of times and
# then gives up and exits non-zero so the failure is visible.
# -----------------------------------------------------------------------------
case "${DATABASE_URL:-}" in
    postgres*)
        if [ "${WAIT_FOR_DB:-true}" = "true" ]; then
            log "waiting for the database to accept connections"
            python - <<'PY'
import os
import sys
import time

import psycopg

dsn = os.environ["DATABASE_URL"]
deadline = time.monotonic() + float(os.environ.get("DB_WAIT_TIMEOUT", "60"))
attempt = 0

while True:
    attempt += 1
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            print(f"[entrypoint] database reachable after {attempt} attempt(s)")
            break
    except Exception as exc:  # noqa: BLE001 - any failure means "not up yet"
        if time.monotonic() >= deadline:
            print(f"[entrypoint] database never became reachable: {exc}", file=sys.stderr)
            sys.exit(1)
        time.sleep(2)
PY
        fi
        ;;
esac

# -----------------------------------------------------------------------------
# Schema and static assets
#
# Running migrations from the entrypoint is fine for a single web process and is
# a mistake the moment there are two replicas: they race, and the loser crashes
# with a duplicate-object error. The production shape is a separate one-shot
# migration job that runs before the new version is rolled out, which is why both
# steps can be switched off here.
# See docs/architecture.md.
# -----------------------------------------------------------------------------
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    log "applying database migrations"
    python manage.py migrate --noinput
fi

if [ "${RUN_COLLECTSTATIC:-true}" = "true" ]; then
    log "collecting static files"
    python manage.py collectstatic --noinput --clear
fi

log "starting: $*"
exec "$@"

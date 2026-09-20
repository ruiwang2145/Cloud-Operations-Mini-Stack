# syntax=docker/dockerfile:1

# =============================================================================
# Stage 1 -- builder
#
# Install dependencies into a virtualenv that is then copied into the runtime
# stage. Anything needed to *build* a wheel stays behind in this layer.
# =============================================================================
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# requirements.txt is copied on its own, before the application code, so that
# editing a Python file does not invalidate the dependency layer. This is the
# single biggest lever on build time in a CI pipeline.
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


# =============================================================================
# Stage 2 -- runtime
# =============================================================================
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Containers log to stdout; the log driver owns rotation and retention.
    LOG_TO_FILE=False \
    LOG_FORMAT=json

# curl exists only to serve the HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Non-root, and created before the code is copied so the COPY can be chowned in
# one step. A process that only serves HTTP has no reason to own its own source
# tree, and "root inside a container" is one kernel bug away from root on the
# host.
RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser . .

RUN chmod +x scripts/docker-entrypoint.sh

# One directory has to be writable, and it is not the source tree.
#
# `collectstatic` writes to STATIC_ROOT, which is /app/staticfiles, and
# .dockerignore keeps staticfiles/ out of the build context -- so the directory
# does not exist in the image.
#
# /app itself was created by WORKDIR as root, and `COPY --chown` only sets the
# ownership of the files it copies, not of the directory they land in. The
# non-root user therefore cannot create anything inside /app, and the container
# dies on every start with
#
#   PermissionError: [Errno 13] Permission denied: '/app/staticfiles'
#
# which `restart: unless-stopped` faithfully turns into a crash loop: the service
# is up for two seconds, restarts, and is never reachable. Creating the single
# directory the application needs to write, and handing over only that one,
# keeps the rest of the tree read-only to the process.
#
# Collecting at build time instead would be the better answer if it were
# possible: SECRET_KEY is required before settings will import, and injecting it
# as a build argument would bake a secret into a layer.
#
# Anything else the process is asked to write needs the same treatment. There are
# two such switches and neither is set here: PROMETHEUS_MULTIPROC_DIR (opt-in,
# see the CMD below) and LOG_FILE, which only applies when LOG_TO_FILE is turned
# on. Setting either one without creating its directory reproduces this exact
# crash loop.
RUN mkdir -p /app/staticfiles \
    && chown appuser:appuser /app/staticfiles

USER appuser

EXPOSE 8000

# The healthcheck probes *liveness*, not readiness.
#
# A container whose database is unreachable is not ready to receive traffic, but
# it is not broken enough to be killed: killing it would turn a database outage
# into a restart loop that hammers the database even harder. Routing decisions
# belong to whatever is reading /readyz/, not to the container runtime.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz/ || exit 1

# Absolute path, not `scripts/docker-entrypoint.sh`.
#
# The exec form of ENTRYPOINT does not go through a shell, so a relative path is
# resolved against the working directory rather than PATH. It happens to work here
# because WORKDIR is /app, but relying on that couples the entrypoint to a WORKDIR
# change three lines up.
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]

# One worker by default, on purpose.
#
# prometheus_client keeps counters in process memory, so with N gunicorn workers
# there are N independent registries. Prometheus would scrape whichever worker
# happened to answer, and the dashboards would show a fraction of the real
# traffic changing unpredictably from scrape to scrape -- metrics that look fine
# and are wrong, which is worse than no metrics.
#
# The fix is prometheus_client's multiprocess mode (set PROMETHEUS_MULTIPROC_DIR;
# the entrypoint prepares the directory and django-prometheus switches to
# MultiProcessCollector automatically). It is documented rather than enabled by
# default because it changes the semantics of gauges, and this project's SLO
# gauges would be summed across workers into nonsense.
# See docs/architecture.md and docs/KNOWN_ISSUES.md.
CMD ["gunicorn", "my_cloudapp.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--timeout", "30", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info"]

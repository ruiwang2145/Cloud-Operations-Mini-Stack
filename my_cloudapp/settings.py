"""Django settings for the Cloud Operations Mini Stack.

Everything that differs between environments is read from the process
environment, so the *same* container image runs locally, in CI and in
production. ``.env`` is a developer convenience only -- in containers the values
are injected by the orchestrator and ``load_dotenv`` is a no-op.

See ``.env.example`` for the complete list of supported variables and
``docs/architecture.md`` for the reasoning behind each decision.
"""
from __future__ import annotations

import os
from pathlib import Path

import dj_database_url
import sentry_sdk
from dotenv import load_dotenv
from sentry_sdk.integrations.django import DjangoIntegration

from ops.sentry_filters import drop_probe_events

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------------- #
# Typed environment helpers
# --------------------------------------------------------------------------- #
# Reading env vars inline is fine for two settings and unmaintainable for forty.
# These four helpers mean a typo produces a working default instead of a crash
# at import time, and the parsing rules are written down exactly once.
def env_bool(name: str, default: bool = False) -> bool:
    return str(os.getenv(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Service identity
# --------------------------------------------------------------------------- #
# These three values are stamped into every log line, every metric and every
# Sentry event. Without them, "is this still broken after the deploy?" is
# unanswerable when two versions are serving traffic during a rolling release.
SERVICE_NAME = os.getenv("SERVICE_NAME", "cloud-ops-mini-stack")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.1.0")
DEPLOY_ENV = os.getenv("DEPLOY_ENV", "dev")

DEBUG = env_bool("DEBUG", False)

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or env_bool("ALLOW_INSECURE_SECRET_KEY", False):
        SECRET_KEY = "django-insecure-development-only-key"
    else:
        # Fail fast and loudly. A service that boots with a guessable signing key
        # is worse than a service that does not boot, because nobody notices.
        raise RuntimeError(
            "SECRET_KEY is not set. Generate one with `python scripts/gen_secret.py` "
            "and write it to .env, or inject it from your secret manager."
        )

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "")


# --------------------------------------------------------------------------- #
# Applications
# --------------------------------------------------------------------------- #
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "django_prometheus",
    # Local
    "ops",
    "tasks",
]

# Middleware order is load-bearing. Read this list top to bottom:
#
#   1. PrometheusBeforeMiddleware starts the framework-level timer.
#   2. RequestIdMiddleware puts a correlation id in the log context *before*
#      anything else can log, so no early request log is missing its id.
#   3. ObservabilityMiddleware wraps almost the entire stack, which is what we
#      want: the access log and the RED metrics describe the whole server-side
#      cost of the request, not just the view function.
#   4. Everything else.
#   5. PrometheusAfterMiddleware stops the timer and records the response.
#
# WhiteNoise is only mounted outside DEBUG. Under `runserver` Django's own
# staticfiles app already serves static assets, and WhiteNoise warns loudly when
# STATIC_ROOT has not been populated yet -- noise that trains people to ignore
# warnings. In a container (DEBUG=False) it is the production static server, and
# the image runs collectstatic before the app starts.
MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "ops.middleware.RequestIdMiddleware",
    "ops.middleware.ObservabilityMiddleware",
    "django.middleware.security.SecurityMiddleware",
    *([] if DEBUG else ["whitenoise.middleware.WhiteNoiseMiddleware"]),
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "my_cloudapp.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "my_cloudapp.wsgi.application"
ASGI_APPLICATION = "my_cloudapp.asgi.application"


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
# One connection string drives every environment: SQLite for a throwaway local
# run, PostgreSQL (Neon serverless) in the cloud. Nothing in the application
# code knows the difference.
DATABASES = {
    "default": dj_database_url.parse(
        os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=env_int("DB_CONN_MAX_AGE", 600),
        ssl_require=False,  # the URL decides; Neon URLs already carry sslmode=require
    )
}

# A readiness probe must fail *fast*. Without a connect timeout, a dead database
# turns /readyz/ into a request that hangs until the TCP stack gives up, and the
# orchestrator's probe timeout fires first -- so the log says "probe timed out"
# instead of "connection refused", and you lose the actual diagnosis.
if DATABASES["default"]["ENGINE"].endswith("postgresql"):
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"].setdefault(
        "connect_timeout", env_int("DB_CONNECT_TIMEOUT", 5)
    )

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --------------------------------------------------------------------------- #
# Internationalisation
# --------------------------------------------------------------------------- #
# Logs and metrics stay in UTC on purpose: correlating a log line with a metric
# sample or a Sentry event is only possible if every source agrees on the clock.
# Local time is a presentation concern, applied at the dashboard.
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------- #
# Static files
# --------------------------------------------------------------------------- #
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # CompressedStaticFilesStorage, not the manifest variant: the manifest
    # storage raises if collectstatic has not run, which turns a forgotten build
    # step into a 500 on the admin. Compression is the part that actually
    # matters here.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}


# --------------------------------------------------------------------------- #
# Structured logging
# --------------------------------------------------------------------------- #
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json").lower()  # json | console
# Local development wants a greppable file. Containers want stdout only, so the
# log driver / Loki / CloudWatch owns rotation and retention instead.
LOG_TO_FILE = env_bool("LOG_TO_FILE", True)

_handlers: list[str] = ["console"]

LOGGING: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        # Guarantees every record carries a request_id, even the ones emitted
        # outside a request cycle (management commands, SLO evaluation).
        "request_context": {"()": "ops.logging.RequestContextFilter"},
    },
    "formatters": {
        "json": {"()": "ops.logging.JsonFormatter"},
        "console": {
            "()": "ops.logging.HumanFormatter",
            "format": "%(asctime)s %(levelname)-8s %(name)-22s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": LOG_FORMAT if LOG_FORMAT in {"json", "console"} else "json",
            "filters": ["request_context"],
        },
    },
    "root": {"handlers": _handlers, "level": LOG_LEVEL},
    "loggers": {
        "app": {"level": LOG_LEVEL, "handlers": _handlers, "propagate": False},
        # Django already logs every 5xx with a traceback; keep it, but at the
        # level the runbooks assume.
        "django.request": {"level": "WARNING", "handlers": _handlers, "propagate": False},
        "django.security": {"level": "WARNING", "handlers": _handlers, "propagate": False},
        "django_prometheus": {"level": "WARNING", "handlers": _handlers, "propagate": False},
    },
}

if LOG_TO_FILE:
    LOGGING["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(BASE_DIR / os.getenv("LOG_FILE", "app.log")),
        "maxBytes": env_int("LOG_MAX_BYTES", 10 * 1024 * 1024),
        "backupCount": env_int("LOG_BACKUP_COUNT", 5),
        "encoding": "utf-8",
        "formatter": "json",
        "filters": ["request_context"],
    }
    _handlers.append("file")
    LOGGING["root"]["handlers"] = _handlers
    for _logger in LOGGING["loggers"].values():
        _logger["handlers"] = _handlers


# --------------------------------------------------------------------------- #
# Django REST Framework
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    # Reads are public so the API can be demonstrated with a plain curl; writes
    # require authentication. This is the smallest permission model that is not
    # simply "allow everything".
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticatedOrReadOnly"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": env_int("API_PAGE_SIZE", 20),
    "DEFAULT_FILTER_BACKENDS": ["rest_framework.filters.OrderingFilter"],
    # A public, unauthenticated endpoint needs a ceiling. Throttling is the
    # cheapest protection against a runaway client and against the load
    # generator being left running by accident.
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.getenv("API_ANON_THROTTLE", "120/min")},
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        *(["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    ],
    # Normalises every error into one envelope and, more importantly, emits a
    # structured log line plus an error metric for each one.
    "EXCEPTION_HANDLER": "ops.exceptions.structured_exception_handler",
}


# --------------------------------------------------------------------------- #
# Prometheus
# --------------------------------------------------------------------------- #
# django-prometheus can spawn its own HTTP server to export metrics from a
# multi-process gunicorn deployment. We deliberately do not use it: metrics are
# exported through the normal Django view on /metrics, which means the same
# routing, the same logging and the same auth story as everything else.
PROMETHEUS_METRICS_EXPORT_PORT_RANGE = None


# --------------------------------------------------------------------------- #
# Service level objectives
# --------------------------------------------------------------------------- #
# These numbers are the contract. docs/SLO.md explains how each one was chosen,
# and monitoring/prometheus/alert_rules.yml is rendered from them by
# scripts/render_rules.py so the alerts cannot drift away from the definition.
SLO_AVAILABILITY_TARGET = env_float("SLO_AVAILABILITY_TARGET", 0.995)
SLO_LATENCY_TARGET_MS = env_int("SLO_LATENCY_TARGET_MS", 300)
SLO_LATENCY_COMPLIANCE_TARGET = env_float("SLO_LATENCY_COMPLIANCE_TARGET", 0.99)
SLO_WINDOW = os.getenv("SLO_WINDOW", "30d")

# Where the SLO reporter reads history from. Empty means "use the in-process
# registry", which is what a local `runserver` session does.
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "").rstrip("/")
PROMETHEUS_QUERY_TIMEOUT = env_float("PROMETHEUS_QUERY_TIMEOUT", 3.0)

# Evaluating the SLO costs two queries against Prometheus. Nothing changes inside
# one scrape interval, so the report is cached for exactly that long.
SLO_CACHE_SECONDS = env_int("SLO_CACHE_SECONDS", 15)


# --------------------------------------------------------------------------- #
# Fault injection
# --------------------------------------------------------------------------- #
# /boom/ and /sentry-debug/ exist to prove that error detection works end to
# end. They are off by default, so a production deployment returns 404 and
# reveals nothing; the incident-drill script turns them on explicitly.
ENABLE_FAULT_ENDPOINTS = env_bool("ENABLE_FAULT_ENDPOINTS", False)


# --------------------------------------------------------------------------- #
# Error tracking
# --------------------------------------------------------------------------- #
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENV = os.getenv("SENTRY_ENV", DEPLOY_ENV)

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        environment=SENTRY_ENV,
        release=f"{SERVICE_NAME}@{SERVICE_VERSION}",
        # Sampling, not "everything": at 100% trace sampling the APM bill grows
        # linearly with traffic while the value flattens out.
        traces_sample_rate=env_float("SENTRY_TRACES_SAMPLE_RATE", 0.2),
        profiles_sample_rate=env_float("SENTRY_PROFILES_SAMPLE_RATE", 0.0),
        # Off by default. Request bodies and headers routinely carry credentials
        # and personal data; shipping them to a third party must be an explicit
        # decision, not a default.
        send_default_pii=env_bool("SENTRY_SEND_PII", False),
        # Health checks run every 15 seconds forever. Without this filter they
        # would be the noisiest thing in the project.
        before_send=drop_probe_events,
    )

"""Settings used by the test suite and by CI.

Imported *instead of* ``my_cloudapp.settings`` (see ``pytest.ini``), so the
environment overrides below are applied before the base module reads them.

The production settings deliberately refuse to start without a ``SECRET_KEY``;
that is the right behaviour for a deployment and the wrong behaviour for a test
run, so the tests set their own.
"""
import os

os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-used-outside-ci")
os.environ.setdefault("LOG_TO_FILE", "False")
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("SENTRY_DSN", "")
os.environ.setdefault("PROMETHEUS_URL", "")
os.environ.setdefault("ENABLE_FAULT_ENDPOINTS", "True")

from .settings import *  # noqa: E402,F401,F403

# Hashing passwords 10,000 times per test user adds seconds to every run and
# tests nothing about the application.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Throttling is a deployment concern; a test that creates 30 tasks must not
# start returning 429.
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {"anon": "10000/min"}}

# Keep the assertion in test_settings.py honest rather than relying on defaults.
assert DEBUG is True

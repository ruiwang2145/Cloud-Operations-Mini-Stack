"""Tests for the fault-injection endpoint gate.

``/boom/`` and ``/sentry-debug/`` exist so the incident drills can prove that
error capture, alerting and the runbooks all work. They must never be reachable
by the public, and "disabled" has to mean 404 rather than 403: a 403 confirms the
endpoint exists and invites someone to keep trying.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from django.test import Client


@pytest.fixture
def disabled(settings):
    settings.ENABLE_FAULT_ENDPOINTS = False
    return settings


@pytest.fixture
def enabled(settings):
    settings.ENABLE_FAULT_ENDPOINTS = True
    return settings


class TestDisabled:
    def test_boom_is_not_found(self, disabled):
        assert Client().get("/boom/").status_code == 404

    def test_sentry_debug_is_not_found(self, disabled):
        assert Client().get("/sentry-debug/").status_code == 404

    def test_version_reports_the_endpoints_as_disabled(self, disabled):
        assert Client().get("/version/").json()["fault_endpoints_enabled"] is False

    def test_a_disabled_endpoint_does_not_raise(self, disabled):
        # The gate has to short-circuit *before* the view body runs, otherwise
        # "disabled" would still produce a 500 and still page someone.
        client = Client()
        client.raise_request_exception = False
        assert client.get("/boom/").status_code == 404


class TestEnabled:
    def test_boom_raises_and_becomes_a_500(self, enabled):
        client = Client()
        client.raise_request_exception = False
        assert client.get("/boom/").status_code == 500

    def test_sentry_debug_raises_a_zero_division_error(self, enabled):
        client = Client()
        client.raise_request_exception = False
        assert client.get("/sentry-debug/").status_code == 500

    def test_version_reports_the_endpoints_as_enabled(self, enabled):
        assert Client().get("/version/").json()["fault_endpoints_enabled"] is True


def test_the_default_setting_is_off():
    """The safe value must be the *default*, not merely documented.

    A line in .env.example is not a control; the code is. This test fails if
    anyone ever flips the default to True for local convenience.
    """
    import my_cloudapp.settings as settings_module

    source = Path(settings_module.__file__).read_text(encoding="utf-8")
    assert 'ENABLE_FAULT_ENDPOINTS = env_bool("ENABLE_FAULT_ENDPOINTS", False)' in source

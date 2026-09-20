"""The alert rules must match the SLO definition.

``monitoring/prometheus/alert_rules.yml`` is generated from
``my_cloudapp/settings.py`` by ``scripts/render_rules.py``. This test is the
mechanism that keeps it that way.

The failure it prevents is specific and nasty: someone changes the availability
objective in settings, the API and the dashboard start reporting the new number,
and the alert keeps firing against the old one. Nothing looks wrong in any of the
three artefacts individually -- and the mismatch only becomes visible during an
incident, when the alert that should have fired does not.

If this test fails, the fix is not to edit the YAML. It is::

    python scripts/render_rules.py

Note: setting an ``SLO_*`` environment variable before running the suite will also
make this fail. That is intended -- the committed rules describe the default
objective, and a deployment that overrides it should regenerate them.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from django.conf import settings

RULES_PATH = Path(settings.BASE_DIR) / "monitoring" / "prometheus" / "alert_rules.yml"
RENDER_SCRIPT = Path(settings.BASE_DIR) / "scripts" / "render_rules.py"
PROMETHEUS_DIR = Path(settings.BASE_DIR) / "monitoring" / "prometheus"


def read_exact(path: Path) -> str:
    """Read without newline translation.

    ``Path.read_text`` normalises CRLF to LF, which would let a file generated on
    Windows pass on Windows and fail in CI on Linux -- the exact class of
    platform-dependent test that wastes an afternoon. ``read_text``'s ``newline``
    parameter is Python 3.13+, and this project supports 3.10+, hence the
    explicit ``open``.
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def renderer():
    """Load scripts/render_rules.py without importing it as a package module."""
    spec = importlib.util.spec_from_file_location("_render_rules_under_test", RENDER_SCRIPT)
    assert spec and spec.loader, f"cannot load {RENDER_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAlertRules:
    def test_the_file_exists(self):
        assert RULES_PATH.exists(), (
            "monitoring/prometheus/alert_rules.yml is missing. "
            "Generate it with: python scripts/render_rules.py"
        )

    def test_the_committed_file_matches_the_rendered_output(self, renderer):
        rendered = renderer.render()
        committed = read_exact(RULES_PATH)
        assert committed == rendered, (
            "alert_rules.yml is out of date with the SLO definition in settings.py.\n"
            "Regenerate it with: python scripts/render_rules.py"
        )

    def test_the_file_uses_lf_line_endings(self):
        # .gitattributes normalises on commit, but a file written with CRLF by a
        # Windows editor before that would still be a real difference in the
        # working tree -- and `render_rules.py --check` would fail on the machine
        # that generated it.
        assert b"\r\n" not in RULES_PATH.read_bytes()

    def test_it_is_valid_yaml_with_rules(self, renderer):
        yaml = pytest.importorskip("yaml")
        payload = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))

        groups = payload["groups"]
        assert groups, "no rule groups defined"

        alerts = {rule["alert"] for group in groups for rule in group["rules"]}
        # The set of alerts is part of the operational contract: the runbooks
        # reference them by name, so a rename has to be deliberate.
        assert alerts == {
            "ServiceDown",
            "AvailabilityBudgetBurnFast",
            "AvailabilityBudgetBurnSlow",
            "HighErrorRate",
            "ErrorBudgetExhausted",
            "LatencyObjectiveBreach",
            "ReadinessCheckFailing",
            "SloReporterDegraded",
            "NoTraffic",
        }

    def test_every_alert_has_a_severity_and_a_runbook(self, renderer):
        yaml = pytest.importorskip("yaml")
        payload = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))

        for group in payload["groups"]:
            for rule in group["rules"]:
                name = rule["alert"]
                assert rule.get("labels", {}).get("severity") in {"critical", "warning"}, name
                assert rule.get("annotations", {}).get("summary"), name
                # An alert with no runbook is an alert somebody has to reverse
                # engineer at 03:00.
                runbook = rule["annotations"].get("runbook", "")
                assert runbook.startswith("docs/runbooks/"), name

    def test_runbooks_referenced_by_alerts_exist(self):
        yaml = pytest.importorskip("yaml")
        payload = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
        base = Path(settings.BASE_DIR)

        for group in payload["groups"]:
            for rule in group["rules"]:
                runbook = rule["annotations"]["runbook"]
                path = base / runbook
                assert path.exists(), f"{rule['alert']} points at a missing runbook: {runbook}"

    def test_every_traffic_rule_excludes_probe_traffic(self):
        yaml = pytest.importorskip("yaml")
        payload = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))

        for group in payload["groups"]:
            for rule in group["rules"]:
                expression = rule["expr"]
                if "app_http_requests_total" not in expression:
                    continue
                # Counting probe traffic would let a monitoring loop that never
                # fails flatter the measured availability, which is the exact
                # opposite of what an alert should do.
                assert "endpoint!~" in expression, rule["alert"]

    def test_the_objective_appears_in_the_rendered_rules(self, renderer):
        rendered = renderer.render()
        objective = settings.SLO_AVAILABILITY_TARGET
        assert f"{objective:.3%}" in rendered
        # The budget is the divisor in the burn-rate expressions.
        assert f"{1.0 - objective:g}" in rendered


class TestScrapeConfigs:
    """The container and native configs must not drift apart.

    `prometheus.yml` (compose, target `web:8000`) and `prometheus.local.yml`
    (native, target `127.0.0.1:8000`) exist because the target hostname differs.
    Everything else about them should be identical, and the thing that matters
    most is that both load the SAME generated rule file -- a native run that
    quietly loaded stale rules would produce exactly the kind of confident,
    wrong evidence a drill is supposed to prevent.
    """

    def test_both_configs_exist(self):
        for name in ("prometheus.yml", "prometheus.local.yml"):
            assert (PROMETHEUS_DIR / name).exists(), name

    def test_both_load_the_generated_rules(self):
        yaml = pytest.importorskip("yaml")
        for name in ("prometheus.yml", "prometheus.local.yml"):
            payload = yaml.safe_load((PROMETHEUS_DIR / name).read_text(encoding="utf-8"))
            rule_files = payload.get("rule_files") or []
            assert rule_files, f"{name} loads no rule files"
            assert any("alert_rules.yml" in entry for entry in rule_files), name

    def test_neither_config_uses_a_trailing_slash_on_the_metrics_path(self):
        # django-prometheus registers the view at `metrics` and Django's
        # APPEND_SLASH does not strip slashes, so `/metrics/` returns 404 and the
        # target silently stays down while the config looks perfectly correct.
        yaml = pytest.importorskip("yaml")
        for name in ("prometheus.yml", "prometheus.local.yml"):
            payload = yaml.safe_load((PROMETHEUS_DIR / name).read_text(encoding="utf-8"))
            for job in payload.get("scrape_configs") or []:
                path = job.get("metrics_path", "/metrics")
                assert not path.endswith("/"), f"{name}: job {job.get('job_name')} -> {path}"

    def test_both_scrape_intervals_match_the_slo_cache(self):
        # There is no point scraping faster than the SLO report refreshes, and no
        # point caching longer than the scrape interval: keeping the two equal
        # means a dashboard and /api/slo/ never disagree by more than one cycle.
        yaml = pytest.importorskip("yaml")
        expected = f"{settings.SLO_CACHE_SECONDS}s"
        for name in ("prometheus.yml", "prometheus.local.yml"):
            payload = yaml.safe_load((PROMETHEUS_DIR / name).read_text(encoding="utf-8"))
            assert payload["global"]["scrape_interval"] == expected, name

    def test_the_local_config_targets_the_loopback(self):
        yaml = pytest.importorskip("yaml")
        payload = yaml.safe_load((PROMETHEUS_DIR / "prometheus.local.yml").read_text(encoding="utf-8"))
        targets = [
            target
            for job in payload["scrape_configs"]
            for config in job.get("static_configs", [])
            for target in config.get("targets", [])
        ]
        # `web:8000` only resolves inside the compose network, so a native run
        # must not reference it.
        assert "127.0.0.1:8000" in targets
        assert not any("web:" in target for target in targets)

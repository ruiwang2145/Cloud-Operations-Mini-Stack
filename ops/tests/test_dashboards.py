"""Guard the Grafana dashboards against metric renames.

A dashboard panel that queries a metric which no longer exists renders as an
*empty* panel. On a quiet service that is indistinguishable from "no traffic",
so a rename in ``ops/metrics.py`` can silently gut the monitoring and nobody
notices until an incident when the graph that was supposed to explain it is
blank.

These tests read the provisioned dashboard JSON, extract every PromQL expression,
and assert that each metric and label it references is actually exported by
``/metrics``. They run against the Django test client, so there is no Prometheus
required and the check happens on every commit.

The converse is not asserted: a metric can be exported and unused without
breaking anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client

DASHBOARD_DIR = Path(settings.BASE_DIR) / "monitoring" / "grafana" / "dashboards"

# The /metrics request travels through the observability middleware, which reads
# `request.user` for the access log -- and that touches the session table. The
# mark is module-wide so every test in this file can use the same fixture.
pytestmark = pytest.mark.django_db

# Labels that Prometheus adds to every series itself, plus the histogram bucket
# boundary label, which exists on `_bucket` samples only.
EXTERNAL_LABELS = {"job", "instance", "le"}

# PromQL keywords and functions that appear in the expressions and are not
# metrics.
PROMQL_KEYWORDS = {
    "sum", "avg", "min", "max", "count", "rate", "irate", "increase", "delta",
    "deriv", "histogram_quantile", "vector", "abs", "clamp_min", "clamp_max",
    "topk", "bottomk", "by", "without", "on", "ignoring", "group_left",
    "group_right", "offset", "bool", "and", "or", "unless", "inf", "nan",
}

METRIC_TOKEN_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")
LABEL_RE = re.compile(r"\{([^}]*)\}")

# Only names in these namespaces are ours to guarantee; anything else is
# Prometheus' own (`up`) or a library's.
OWNED_PREFIXES = ("app_", "django_", "python_")


def _stem(name: str) -> str:
    """Reduce a metric to its family name.

    ``app_http_request_duration_seconds_bucket`` and
    ``app_http_request_duration_seconds_count`` are both samples of the family
    ``app_http_request_duration_seconds``; a dashboard may legitimately reference
    either. Comparing families avoids a test that fails on a naming detail rather
    than on a real mismatch.
    """
    for suffix in ("_bucket", "_count", "_sum", "_created", "_total"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


@pytest.fixture
def exported() -> tuple[set[str], set[str]]:
    """Every metric family and label key the application exports."""
    Client().get("/api/tasks/")  # make sure the request path has emitted samples
    body = Client().get("/metrics").content.decode()

    families: set[str] = set()
    labels: set[str] = set()

    for line in body.splitlines():
        if line.startswith("# TYPE "):
            families.add(_stem(line.split()[2]))
            continue
        if not line or line.startswith("#"):
            continue

        if "{" in line:
            name, rest = line.split("{", 1)
            families.add(_stem(name.strip()))
            for pair in rest.split("}", 1)[0].split(","):
                if "=" in pair:
                    labels.add(pair.split("=", 1)[0].strip())
        else:
            families.add(_stem(line.split()[0]))

    return families, labels


def dashboard_files() -> list[Path]:
    files = sorted(DASHBOARD_DIR.glob("*.json"))
    assert files, f"no dashboards found in {DASHBOARD_DIR}"
    return files


def expressions(payload: dict) -> list[tuple[str, str]]:
    """Every PromQL expression in a dashboard, with its panel title."""
    found: list[tuple[str, str]] = []
    for panel in payload.get("panels", []):
        for target in panel.get("targets", []):
            if target.get("expr"):
                found.append((panel.get("title", "<untitled>"), target["expr"]))
    for variable in payload.get("templating", {}).get("list", []):
        query = variable.get("query")
        if isinstance(query, dict) and query.get("query"):
            found.append((f"variable:{variable['name']}", query["query"]))
    return found


class TestDashboardFiles:
    def test_every_file_is_valid_json(self):
        for path in dashboard_files():
            json.loads(path.read_text(encoding="utf-8"))

    def test_uids_are_unique_and_stable(self):
        uids = [
            json.loads(path.read_text(encoding="utf-8"))["uid"] for path in dashboard_files()
        ]
        assert len(uids) == len(set(uids))

    def test_panels_fit_the_grid(self):
        # Grafana's grid is 24 columns wide. A panel placed outside it is silently
        # pushed onto another row and the layout looks broken to a reviewer.
        for path in dashboard_files():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for panel in payload["panels"]:
                grid = panel["gridPos"]
                assert 0 <= grid["x"] < 24, (path.name, panel["title"])
                assert grid["x"] + grid["w"] <= 24, (path.name, panel["title"])
                assert grid["w"] > 0 and grid["h"] > 0

    def test_panel_ids_are_unique(self):
        for path in dashboard_files():
            payload = json.loads(path.read_text(encoding="utf-8"))
            ids = [panel["id"] for panel in payload["panels"]]
            assert len(ids) == len(set(ids)), path.name

    def test_every_panel_has_a_description(self):
        # A dashboard panel without a description is a number without a meaning.
        # Requiring one is a cheap way to keep the dashboards explainable.
        for path in dashboard_files():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for panel in payload["panels"]:
                if panel["type"] == "text":
                    continue
                assert panel.get("description"), (path.name, panel["title"])


class TestDashboardQueries:
    def test_every_referenced_metric_is_exported(self, exported):
        families, _ = exported
        missing: list[str] = []

        for path in dashboard_files():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for title, expr in expressions(payload):
                for token in METRIC_TOKEN_RE.findall(expr):
                    if token in PROMQL_KEYWORDS or not token.startswith(OWNED_PREFIXES):
                        continue
                    if _stem(token) not in families:
                        missing.append(f"{path.name} :: {title} :: {token}")

        assert not missing, "dashboards reference metrics that are not exported:\n" + "\n".join(
            sorted(set(missing))
        )

    def test_every_referenced_label_exists(self, exported):
        _, labels = exported
        unknown: list[str] = []

        for path in dashboard_files():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for title, expr in expressions(payload):
                for block in LABEL_RE.findall(expr):
                    for pair in block.split(","):
                        if "=" not in pair:
                            continue
                        key = pair.split("=", 1)[0].strip()
                        if key in EXTERNAL_LABELS or key in labels:
                            continue
                        # A template variable is not a label.
                        if key.startswith("$"):
                            continue
                        unknown.append(f"{path.name} :: {title} :: {key}")

        assert not unknown, "dashboards reference labels that do not exist:\n" + "\n".join(
            sorted(set(unknown))
        )

    def test_probe_endpoints_are_excluded_from_every_traffic_query(self, exported):
        """The SLI exclusion has to hold in the dashboards too.

        If a panel counted /healthz/ and /readyz/, it would disagree with
        /api/slo/ and the alert rules while looking perfectly reasonable.
        """
        offenders: list[str] = []

        for path in dashboard_files():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for title, expr in expressions(payload):
                if "app_http_requests_total" not in expr and "app_http_request_duration" not in expr:
                    continue
                if "endpoint=~" in expr and "$endpoint" in expr:
                    continue  # the detail dashboard filters via its variable
                if "endpoint!~" not in expr:
                    offenders.append(f"{path.name} :: {title}")

        assert not offenders, (
            "these panels aggregate request metrics without excluding probe traffic:\n"
            + "\n".join(sorted(set(offenders)))
        )

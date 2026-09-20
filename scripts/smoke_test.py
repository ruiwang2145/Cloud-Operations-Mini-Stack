#!/usr/bin/env python
"""End-to-end smoke test against a running instance.

    python scripts/smoke_test.py
    python scripts/smoke_test.py --base-url http://localhost:8000
    python scripts/smoke_test.py --json          # machine-readable, for CI

What this is for
----------------
A test suite proves the code works in a test database with a test client. It does
not prove that the *deployed* service answers on the right port, with the right
environment, behind the right proxy, with the right migrations applied. Those are
different failure modes and they are the ones that actually happen.

So this script drives the real HTTP surface and asserts the things an operator
would check by hand, in the order they would check them. When something is wrong
it says which endpoint, what it expected, what it got, and how long it took --
which is the difference between a five-minute diagnosis and a twenty-minute one.

Deliberately stdlib-only: it has to be runnable against a production host, where
installing `requests` may not be possible, and a diagnostic tool that needs a
dependency is a diagnostic tool that is not there when you need it.

One implementation note: requests go over a *reused* connection rather than one
`urlopen` per check. A fresh TCP handshake per request costs ~200 ms on a Windows
loopback and far more across a network, which would swamp the numbers this script
exists to report. Reusing the socket also means the timings measure the service
rather than the test harness.
"""
from __future__ import annotations

import argparse
import http.client
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# Metric families the dashboards and alert rules depend on. A rename here without
# a rename in monitoring/ would empty a panel or silently disable an alert, so the
# smoke test refuses to call the service healthy when they are missing.
REQUIRED_METRICS = (
    "app_http_requests_total",
    "app_http_request_duration_seconds_bucket",
    "app_errors_total",
    "app_readiness_failures_total",
    "app_slo_availability_ratio",
    "app_slo_error_budget_remaining_ratio",
    "app_slo_burn_rate",
    "app_build_info",
    "django_http_responses_total_by_status_view_method_total",
    "django_http_requests_latency_seconds_by_view_method_bucket",
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: float = 0.0


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: str
    duration_ms: float

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return None


@dataclass
class Runner:
    base_url: str
    timeout: float = 5.0
    request_id: str = "smoke-test"
    checks: list[Check] = field(default_factory=list)

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if not parts.hostname:
            raise SystemExit(f"error: {self.base_url!r} is not a valid base URL")
        self._scheme = parts.scheme or "http"
        self._host = parts.hostname
        self._port = parts.port or (443 if self._scheme == "https" else 80)
        self._prefix = parts.path.rstrip("/")
        self._connection: http.client.HTTPConnection | None = None

    # -- HTTP ---------------------------------------------------------------
    def _connect(self) -> http.client.HTTPConnection:
        if self._connection is None:
            factory = (
                http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
            )
            self._connection = factory(self._host, self._port, timeout=self.timeout)
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def request(self, method: str, path: str, body: Any = None, _retry: bool = True) -> Response:
        headers = {
            "Accept": "application/json",
            "X-Request-ID": self.request_id,
            "User-Agent": "cloud-ops-smoke-test/1.0",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        connection = self._connect()
        started = time.perf_counter()
        try:
            connection.request(method, f"{self._prefix}{path}", body=payload, headers=headers)
            raw = connection.getresponse()
            text = raw.read().decode("utf-8", errors="replace")
            status = raw.status
            response_headers = {key.lower(): value for key, value in raw.getheaders()}
        except http.client.RemoteDisconnected:
            # Servers and proxies close idle keep-alive connections. Dropping the
            # socket and retrying once is what any HTTP client does; without it a
            # long-running smoke test fails on the first check after a pause.
            self.close()
            if _retry:
                return self.request(method, path, body, _retry=False)
            raise
        except (http.client.HTTPException, OSError) as exc:
            self.close()
            raise SystemExit(
                f"error: cannot reach {self.base_url}{path} ({exc}).\n"
                f"       Is the service running? Try: python manage.py runserver"
            ) from exc

        return Response(
            status=status,
            headers=response_headers,
            body=text,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    # -- helpers ------------------------------------------------------------
    def check(self, name: str, ok: bool, detail: str = "", duration_ms: float = 0.0) -> Check:
        result = Check(name=name, ok=ok, detail=detail, duration_ms=duration_ms)
        self.checks.append(result)
        return result

    # -- the checks ---------------------------------------------------------
    def run(self) -> None:
        self._check_liveness()
        self._check_readiness()
        self._check_version()
        self._check_metrics()
        self._check_metrics_trailing_slash()
        self._check_api_list()
        self._check_api_write_requires_auth()
        self._check_api_validation_envelope()
        self._check_slo_report()
        self._check_request_id_correlation()
        self._check_unknown_path_is_bounded()

    def _check_liveness(self) -> None:
        response = self.request("GET", "/healthz/")
        payload = response.json() or {}
        self.check(
            "liveness returns 200 and reports ok",
            response.status == 200 and payload.get("status") == "ok",
            f"status={response.status} body={payload.get('status')!r}",
            response.duration_ms,
        )

    def _check_readiness(self) -> None:
        response = self.request("GET", "/readyz/")
        payload = response.json() or {}
        database = (payload.get("checks") or {}).get("database") or {}
        self.check(
            "readiness reports every dependency as reachable",
            response.status == 200 and payload.get("status") == "ready",
            f"status={response.status} database={database.get('ok')} "
            f"latency_ms={database.get('latency_ms')}",
            response.duration_ms,
        )

    def _check_version(self) -> None:
        response = self.request("GET", "/version/")
        payload = response.json() or {}
        self.check(
            "version endpoint reports the build",
            response.status == 200 and bool(payload.get("version")),
            f"service={payload.get('service')} version={payload.get('version')} "
            f"env={payload.get('environment')}",
            response.duration_ms,
        )

    def _check_metrics(self) -> None:
        response = self.request("GET", "/metrics")
        missing = [name for name in REQUIRED_METRICS if name not in response.body]
        self.check(
            "metrics endpoint exports every required family",
            response.status == 200 and not missing,
            f"status={response.status} bytes={len(response.body)} "
            + (f"MISSING={missing}" if missing else f"({len(REQUIRED_METRICS)} families present)"),
            response.duration_ms,
        )

    def _check_metrics_trailing_slash(self) -> None:
        # Prometheus scrapes the literal path from its configuration. django-
        # prometheus registers `metrics` without a slash and Django's APPEND_SLASH
        # does not strip one, so `/metrics/` 404s. Asserting it here documents the
        # behaviour and catches anyone "helpfully" adding a slash later.
        response = self.request("GET", "/metrics/")
        self.check(
            "metrics is served without a trailing slash",
            response.status == 404,
            f"GET /metrics/ -> {response.status} (expected 404)",
            response.duration_ms,
        )

    def _check_api_list(self) -> None:
        response = self.request("GET", "/api/tasks/")
        payload = response.json() or {}
        paginated = all(key in payload for key in ("count", "results"))
        self.check(
            "task list is paginated and readable without authentication",
            response.status == 200 and paginated,
            f"status={response.status} count={payload.get('count')}",
            response.duration_ms,
        )

    def _check_api_write_requires_auth(self) -> None:
        response = self.request("POST", "/api/tasks/", body={"title": "smoke test"})
        payload = response.json() or {}
        envelope = (payload.get("error") or {})
        self.check(
            "anonymous writes are refused with the standard error envelope",
            response.status in (401, 403) and envelope.get("type") == "NotAuthenticated",
            f"status={response.status} error.type={envelope.get('type')!r}",
            response.duration_ms,
        )

    def _check_api_validation_envelope(self) -> None:
        # Unauthenticated, so this asserts the envelope shape rather than the
        # validation itself. The validation path is covered by the test suite.
        response = self.request("POST", "/api/tasks/", body={"title": "   "})
        payload = response.json() or {}
        error = payload.get("error") or {}
        self.check(
            "errors carry a type, a status and a request id",
            set(payload.keys()) == {"error"}
            and {"type", "status", "detail", "request_id"} <= set(error.keys())
            and bool(error.get("request_id")),
            f"keys={sorted(error.keys())} request_id={error.get('request_id')}",
            response.duration_ms,
        )

    def _check_slo_report(self) -> None:
        response = self.request("GET", "/api/slo/")
        payload = response.json() or {}
        availability = payload.get("availability") or {}
        latency = payload.get("latency") or {}
        objectives = payload.get("objectives") or {}

        coherent = (
            response.status == 200
            and "objectives" in payload
            and "status" in payload
            and "source" in payload
            # A report must never claim to be healthy while an objective is
            # marked as unmet -- that is the contradiction that destroys trust in
            # a dashboard.
            and not (payload.get("status") == "healthy" and availability.get("met") is False)
            and not (payload.get("status") == "healthy" and latency.get("met") is False)
        )
        self.check(
            "SLO report is present and internally consistent",
            coherent,
            f"status={response.status} slo={payload.get('status')} "
            f"availability={availability.get('sli')} source={payload.get('source')} "
            f"window={payload.get('window')} (from {payload.get('window_source')}) "
            f"target={objectives.get('availability')}",
            response.duration_ms,
        )

    def _check_request_id_correlation(self) -> None:
        response = self.request("GET", "/healthz/")
        echoed = response.headers.get("x-request-id")
        self.check(
            "the caller's request id is echoed back",
            echoed == self.request_id,
            f"sent={self.request_id!r} received={echoed!r}",
            response.duration_ms,
        )

    def _check_unknown_path_is_bounded(self) -> None:
        # Not a functional requirement, a monitoring one: unknown paths must all
        # collapse into one metric series. Asserted here because the failure mode
        # (a cardinality explosion) only shows up in production, under load.
        first = self.request("GET", "/definitely-not-a-route-1")
        second = self.request("GET", "/definitely-not-a-route-2")
        metrics = self.request("GET", "/metrics").body
        unmatched_series = [
            line for line in metrics.splitlines()
            if line.startswith("app_http_requests_total{") and 'endpoint="unmatched"' in line
        ]
        self.check(
            "unknown paths collapse into a single 'unmatched' series",
            first.status == 404 and second.status == 404 and len(unmatched_series) >= 1,
            f"both 404, unmatched series={len(unmatched_series)}",
            first.duration_ms + second.duration_ms,
        )


def report(checks: list[Check], base_url: str, as_json: bool) -> int:
    passed = sum(1 for check in checks if check.ok)
    failed = len(checks) - passed

    if as_json:
        print(
            json.dumps(
                {
                    "target": base_url,
                    "passed": passed,
                    "failed": failed,
                    "checks": [
                        {"name": c.name, "ok": c.ok, "detail": c.detail,
                         "duration_ms": round(c.duration_ms, 2)}
                        for c in checks
                    ],
                },
                indent=2,
            )
        )
        return 1 if failed else 0

    print("Cloud Ops Mini Stack -- smoke test")
    print(f"target  {base_url}")
    print()

    width = max(len(check.name) for check in checks)
    for check in checks:
        marker = "PASS" if check.ok else "FAIL"
        print(f"  [{marker}] {check.name:<{width}}  {check.duration_ms:7.1f} ms")
        if not check.ok or check.detail:
            print(f"         {check.detail}")

    total_ms = sum(check.duration_ms for check in checks)
    print()
    if failed:
        print(f"  {passed} passed, {failed} FAILED  ({total_ms:.0f} ms)")
    else:
        print(f"  {passed} passed, 0 failed  ({total_ms:.0f} ms)")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="root URL of the running service (default: %(default)s)",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    parser.add_argument("--json", action="store_true", help="emit a JSON report instead of a table")
    args = parser.parse_args()

    runner = Runner(base_url=args.base_url, timeout=args.timeout)
    try:
        runner.run()
    finally:
        runner.close()
    return report(runner.checks, args.base_url, args.json)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Run an incident drill and measure how long detection actually took.

    python scripts/run_drill.py --drill unhandled-exception
    python scripts/run_drill.py --drill unknown-path-scan
    python scripts/run_drill.py --drill sentry-path --log-file app.log
    python scripts/run_drill.py --drill readiness-failure      # prints the manual steps

Why drill instead of trusting the setup
---------------------------------------
A monitoring stack that has never seen a failure is a hypothesis. Nobody knows
whether the alert fires, whether the exception reaches Sentry, whether the log
line carries the request id, or how long any of it takes -- until something breaks
in production, which is the worst possible moment to find out.

A drill makes one failure happen on purpose, in a controlled way, and measures the
path from cause to evidence:

    inject -> detect in the metrics -> correlate in the logs -> confirm in the SLO

Every step is timed, and the output is a markdown section that can be pasted into
``docs/incident-drills.md``. Numbers measured rather than asserted, because
"monitoring works" is not a fact until it has been observed.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_test import Runner  # noqa: E402  (same directory, shared HTTP client)

SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)$")


# --------------------------------------------------------------------------- #
# Metrics parsing
# --------------------------------------------------------------------------- #
class Metrics:
    """A snapshot of a /metrics response, addressable by name and labels."""

    def __init__(self, text: str) -> None:
        self.samples: list[tuple[str, dict[str, str], float]] = []
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            match = SAMPLE_RE.match(line)
            if not match:
                continue
            name, raw_labels, raw_value = match.groups()
            labels: dict[str, str] = {}
            if raw_labels:
                for pair in raw_labels.strip("{}").split(","):
                    if "=" in pair:
                        key, _, value = pair.partition("=")
                        labels[key.strip()] = value.strip().strip('"')
            try:
                value = float(raw_value)
            except ValueError:
                continue
            self.samples.append((name, labels, value))

    def value(self, name: str, **labels: str) -> float:
        wanted = {key: str(value) for key, value in labels.items()}
        total = 0.0
        for sample_name, sample_labels, value in self.samples:
            if sample_name != name:
                continue
            if all(sample_labels.get(key) == expected for key, expected in wanted.items()):
                total += value
        return total

    def series(self, name: str) -> list[dict[str, str]]:
        return [labels for sample_name, labels, _ in self.samples if sample_name == name]


# --------------------------------------------------------------------------- #
# Drill results
# --------------------------------------------------------------------------- #
@dataclass
class DrillResult:
    name: str
    title: str
    injected: str
    detected: str
    detection_latency_s: float | None = None
    evidence: list[str] = field(default_factory=list)
    correlated: bool | None = None
    passed: bool = False
    notes: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        lines = [
            f"### Drill: {self.title}",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Injected | {self.injected} |",
            f"| Detected by | {self.detected} |",
            "| Time to detect | "
            + (f"{self.detection_latency_s:.2f} s" if self.detection_latency_s is not None else "n/a")
            + " |",
            "| Log correlation | "
            + ("request id matched" if self.correlated else "not checked" if self.correlated is None else "**NOT FOUND**")
            + " |",
            f"| Outcome | {'pass' if self.passed else 'FAIL'} |",
            "",
        ]
        if self.evidence:
            lines.append("Evidence:")
            lines.append("")
            for item in self.evidence:
                lines.append(f"- `{item}`")
            lines.append("")
        if self.notes:
            for note in self.notes:
                lines.append(f"> {note}")
            lines.append("")
        return "\n".join(lines)


def poll_until(
    runner: Runner, predicate, timeout: float, interval: float = 0.5
) -> tuple[bool, float]:
    """Poll /metrics until ``predicate(Metrics)`` is true. Returns (found, seconds)."""
    started = time.perf_counter()
    while True:
        metrics = Metrics(runner.request("GET", "/metrics").body)
        if predicate(metrics):
            return True, time.perf_counter() - started
        if time.perf_counter() - started >= timeout:
            return False, time.perf_counter() - started
        time.sleep(interval)


def log_has_request_id(log_file: Path, request_id: str, message: str) -> tuple[bool, str]:
    """Look for a structured log line carrying this request id.

    This is the payoff of the whole correlation design: the id sent by the client
    appears verbatim in the server's log, so a user's bug report and the server's
    stack trace are joinable on one value.
    """
    if not log_file.exists():
        return False, f"{log_file} not found"
    found = False
    detail = ""
    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if request_id not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("request_id") == request_id:
            found = True
            if event.get("message") == message:
                detail = (
                    f"message={event['message']} endpoint={event.get('endpoint')} "
                    f"error_type={event.get('error_type')}"
                )
                break
    return found, detail


# --------------------------------------------------------------------------- #
# Drills
# --------------------------------------------------------------------------- #
def drill_unhandled_exception(runner: Runner, args) -> DrillResult:
    result = DrillResult(
        name="unhandled-exception",
        title="Unhandled exception is captured, counted and correlated",
        injected="`GET /boom/` (raises `RuntimeError`)",
        detected="`app_errors_total` in `/metrics`",
    )

    runner.request_id = f"drill-boom-{random.randint(1000, 9999)}"
    before = Metrics(runner.request("GET", "/metrics").body)
    baseline = before.value(
        "app_errors_total", endpoint="/boom/", error_type="RuntimeError"
    )
    result.evidence.append(
        f"app_errors_total{{endpoint=\"/boom/\",error_type=\"RuntimeError\"}} before = {baseline:g}"
    )

    response = runner.request("GET", "/boom/")
    if response.status != 500:
        result.notes.append(
            f"`/boom/` returned {response.status} instead of 500. Are the fault endpoints "
            f"enabled (`ENABLE_FAULT_ENDPOINTS=true`)?"
        )
        return result
    result.evidence.append(f"GET /boom/ -> {response.status} in {response.duration_ms:.0f} ms")

    found, elapsed = poll_until(
        runner,
        lambda metrics: metrics.value(
            "app_errors_total", endpoint="/boom/", error_type="RuntimeError"
        )
        > baseline,
        timeout=args.detect_timeout,
    )
    result.detection_latency_s = elapsed

    after = Metrics(runner.request("GET", "/metrics").body)
    counted = after.value("app_errors_total", endpoint="/boom/", error_type="RuntimeError")
    result.evidence.append(
        f"app_errors_total{{endpoint=\"/boom/\",error_type=\"RuntimeError\"}} after = {counted:g} "
        f"(delta {counted - baseline:g})"
    )
    # Exactly one increment per injected failure: the process_exception hook and
    # the response path must not both count the same error.
    if counted - baseline != 1:
        result.notes.append(
            f"Expected the counter to rise by exactly 1, it rose by {counted - baseline:g}. "
            f"Two observers are counting the same failure."
        )

    result.correlated, detail = log_has_request_id(
        args.log_file, runner.request_id, "unhandled_exception"
    )
    if detail:
        result.evidence.append(f"log line: {detail}")

    result.passed = found and (counted - baseline) == 1
    return result


def drill_sentry_path(runner: Runner, args) -> DrillResult:
    result = DrillResult(
        name="sentry-path",
        title="A second failure mode follows the same detection path",
        injected="`GET /sentry-debug/` (raises `ZeroDivisionError`)",
        detected="`app_errors_total` in `/metrics`",
    )

    runner.request_id = f"drill-sentry-{random.randint(1000, 9999)}"
    before = Metrics(runner.request("GET", "/metrics").body)
    baseline = before.value(
        "app_errors_total", endpoint="/sentry-debug/", error_type="ZeroDivisionError"
    )

    response = runner.request("GET", "/sentry-debug/")
    if response.status != 500:
        result.notes.append(
            f"`/sentry-debug/` returned {response.status} instead of 500. Fault endpoints enabled?"
        )
        return result
    result.evidence.append(f"GET /sentry-debug/ -> {response.status}")

    found, elapsed = poll_until(
        runner,
        lambda metrics: metrics.value(
            "app_errors_total", endpoint="/sentry-debug/", error_type="ZeroDivisionError"
        )
        > baseline,
        timeout=args.detect_timeout,
    )
    result.detection_latency_s = elapsed

    after = Metrics(runner.request("GET", "/metrics").body)
    counted = after.value(
        "app_errors_total", endpoint="/sentry-debug/", error_type="ZeroDivisionError"
    )
    result.evidence.append(
        f"app_errors_total{{error_type=\"ZeroDivisionError\"}} = {counted:g} "
        f"(delta {counted - baseline:g})"
    )

    result.correlated, detail = log_has_request_id(
        args.log_file, runner.request_id, "unhandled_exception"
    )
    if detail:
        result.evidence.append(f"log line: {detail}")

    result.passed = found and (counted - baseline) == 1
    return result


def drill_unknown_path_scan(runner: Runner, args) -> DrillResult:
    """A scanner walking the site must not create one time series per URL."""
    result = DrillResult(
        name="unknown-path-scan",
        title="Unknown paths collapse into one bounded metric series",
        injected=f"{args.scan_requests} requests for random non-existent paths",
        detected="`app_http_requests_total{endpoint=\"unmatched\"}`",
    )

    before = Metrics(runner.request("GET", "/metrics").body)
    baseline = before.value(
        "app_http_requests_total", endpoint="unmatched", status_class="4xx"
    )

    rng = random.Random(args.seed)
    started = time.perf_counter()
    for _ in range(args.scan_requests):
        runner.request("GET", f"/{rng.choice(['admin.php', 'wp-login.php', '.env', 'backup.zip'])}-{rng.randint(1, 10**6)}")
    elapsed = time.perf_counter() - started

    after = Metrics(runner.request("GET", "/metrics").body)
    counted = after.value(
        "app_http_requests_total", endpoint="unmatched", status_class="4xx"
    )
    result.detection_latency_s = elapsed
    result.evidence.append(
        f"app_http_requests_total{{endpoint=\"unmatched\"}} delta = {counted - baseline:g} "
        f"for {args.scan_requests} distinct URLs"
    )
    distinct_unmatched = sum(
        1
        for labels in after.series("app_http_requests_total")
        if labels.get("endpoint") == "unmatched"
    )
    result.evidence.append(f"distinct `unmatched` series = {distinct_unmatched}")

    # The whole point: 500 distinct URLs, still exactly one series per method and
    # status class. If this grows with the number of URLs, the label is a raw path.
    result.passed = (counted - baseline) == args.scan_requests and distinct_unmatched <= 2
    if not result.passed:
        result.notes.append(
            "The `unmatched` label is no longer collapsing distinct URLs. Check "
            "`ops/endpoints.py::normalise_endpoint`."
        )
    return result


def drill_readiness_failure(runner: Runner, args) -> DrillResult:
    """Cannot be triggered over HTTP -- it needs the dependency to break."""
    result = DrillResult(
        name="readiness-failure",
        title="Readiness probe takes the instance out of rotation (manual)",
        injected="stop PostgreSQL, or point `DATABASE_URL` at a dead host",
        detected="`/readyz/` returns 503; `app_readiness_failures_total` rises",
    )
    result.notes.append(
        "This drill cannot be driven from outside the service: it requires a dependency to "
        "actually fail. Procedure: stop the database container, then poll `/readyz/` until it "
        "returns 503, and confirm `up` stayed 1 and `app_readiness_failures_total` increased. "
        "The instance must stay alive -- liveness is what would trigger a restart, and it "
        "deliberately does not touch the database."
    )
    response = runner.request("GET", "/readyz/")
    result.evidence.append(
        f"baseline: /readyz/ -> {response.status} ({(response.json() or {}).get('status')})"
    )
    result.notes.append(
        "See `docs/runbooks/database-unreachable.md` for the full procedure and the expected "
        "timeline."
    )
    result.passed = response.status == 200
    return result


DRILLS = {
    "unhandled-exception": drill_unhandled_exception,
    "sentry-path": drill_sentry_path,
    "unknown-path-scan": drill_unknown_path_scan,
    "readiness-failure": drill_readiness_failure,
}


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--drill", choices=sorted(DRILLS), default="unhandled-exception")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--log-file", type=Path, default=Path("app.log"),
                        help="structured log to search for the request id (default: app.log)")
    parser.add_argument("--detect-timeout", type=float, default=15.0,
                        help="how long to poll /metrics before calling detection a failure")
    parser.add_argument("--scan-requests", type=int, default=200,
                        help="number of unknown paths for the scan drill")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    runner = Runner(base_url=args.base_url)
    try:
        health = runner.request("GET", "/healthz/")
        if health.status != 200:
            print(f"error: {args.base_url}/healthz/ returned {health.status}", file=sys.stderr)
            return 2

        result = DRILLS[args.drill](runner, args)
    finally:
        runner.close()

    if args.json:
        print(
            json.dumps(
                {
                    "drill": result.name,
                    "passed": result.passed,
                    "detection_latency_s": result.detection_latency_s,
                    "correlated": result.correlated,
                    "evidence": result.evidence,
                    "notes": result.notes,
                },
                indent=2,
            )
        )
        return 0 if result.passed else 1

    print("# Incident drill report\n")
    print(f"Target: `{args.base_url}`  ")
    print(f"Run at: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    print(result.markdown())
    print(f"**Result: {'PASS' if result.passed else 'FAIL'}**\n")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

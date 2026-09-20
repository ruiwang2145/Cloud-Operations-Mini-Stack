#!/usr/bin/env python
"""Generate a realistic traffic mix against a running instance.

    python scripts/loadgen.py
    TARGET_URL=http://web:8000 LOADGEN_RPS=8 python scripts/loadgen.py

Why this exists
---------------
An empty dashboard proves nothing. Every panel on the overview dashboard reads
"no data" until something actually calls the service, and the SLO reporter returns
``no_data`` for the same reason. A reviewer who runs `docker compose up` and finds
fourteen blank panels has learned nothing about whether the monitoring works.

So this drives a mix that exercises each part of the pipeline on purpose:

* reads that succeed and produce latency samples,
* writes that are refused with a 403, producing 4xx traffic that must **not**
  count against availability,
* unhandled failures, producing 5xx traffic that must,
* probe calls, which must be excluded from the SLI entirely.

If a change ever breaks the 4xx/5xx distinction or the probe exclusion, the
dashboards will show it immediately -- which is a faster signal than reading the
code.

Stdlib only, so it can run inside a minimal container.
"""
from __future__ import annotations

import http.client
import json
import os
import random
import signal
import sys
import threading
import time
from urllib.parse import urlsplit

TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8000")
RPS = float(os.getenv("LOADGEN_RPS", "4"))
ERROR_RATIO = float(os.getenv("LOADGEN_ERROR_RATIO", "0.05"))
DURATION = float(os.getenv("LOADGEN_DURATION", "0"))  # 0 = run until stopped
TIMEOUT = float(os.getenv("LOADGEN_TIMEOUT", "5"))
SEED = os.getenv("LOADGEN_SEED")

_stop = threading.Event()


def _install_signal_handlers() -> None:
    def handler(signum, _frame):
        print(f"\nloadgen: signal {signum}, finishing in-flight requests...")
        _stop.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


class Client:
    """One connection per worker thread.

    ``http.client`` is not thread-safe, and a shared connection would interleave
    responses from different requests -- the kind of bug that shows up as
    occasional nonsense 400s and takes an afternoon to find.
    """

    def __init__(self, base_url: str, seed: int | None = None) -> None:
        parts = urlsplit(base_url)
        self.host = parts.hostname or "localhost"
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.secure = parts.scheme == "https"
        self.prefix = parts.path.rstrip("/")
        self.rng = random.Random(seed)
        self.connection: http.client.HTTPConnection | None = None

    def _connect(self) -> http.client.HTTPConnection:
        if self.connection is None:
            factory = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
            self.connection = factory(self.host, self.port, timeout=TIMEOUT)
        return self.connection

    def send(self, method: str, path: str, body: dict | None = None) -> int:
        payload = None
        headers = {"Accept": "application/json", "User-Agent": "cloud-ops-loadgen/1.0"}
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        for attempt in range(2):
            try:
                connection = self._connect()
                connection.request(method, f"{self.prefix}{path}", body=payload, headers=headers)
                response = connection.getresponse()
                response.read()
                return response.status
            except (http.client.HTTPException, OSError):
                # Reconnect once. A dropped keep-alive is normal; a connection
                # refused is not, and the next attempt will raise again and be
                # counted as an error.
                if self.connection is not None:
                    self.connection.close()
                    self.connection = None
                if attempt == 1:
                    return 0
        return 0

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counts: dict[int, int] = {}
        self.errors = 0
        self.total = 0

    def record(self, status: int) -> None:
        with self.lock:
            self.total += 1
            if status == 0:
                self.errors += 1
            else:
                self.counts[status] = self.counts.get(status, 0) + 1

    def summary(self) -> str:
        with self.lock:
            by_status = ", ".join(f"{code}:{count}" for code, count in sorted(self.counts.items()))
            return (
                f"{self.total} requests"
                + (f" ({by_status})" if by_status else "")
                + (f", {self.errors} connection error(s)" if self.errors else "")
            )


def worker(client: Client, stats: Stats, sleep_between: float) -> None:
    while not _stop.is_set():
        roll = client.rng.random()

        if roll < ERROR_RATIO:
            # A real 5xx, so the error rate panel and the alert rules have
            # something to fire on. 404 if fault endpoints are disabled, which is
            # the correct production behaviour.
            status = client.send("GET", "/boom/")
        elif roll < ERROR_RATIO + 0.10:
            status = client.send("GET", "/healthz/")          # probe traffic: excluded
        elif roll < ERROR_RATIO + 0.20:
            status = client.send("GET", "/api/slo/")
        elif roll < ERROR_RATIO + 0.32:
            status = client.send(
                "POST", "/api/tasks/", {"title": f"loadgen {time.time():.3f}"}
            )                                                  # 403: client error
        elif roll < ERROR_RATIO + 0.50:
            status = client.send("GET", f"/api/tasks/{client.rng.randint(1, 50)}/")
        else:
            status = client.send("GET", "/api/tasks/?page=1")

        stats.record(status)
        if sleep_between > 0:
            time.sleep(sleep_between)


def main() -> int:
    _install_signal_handlers()

    workers = max(2, min(8, int(RPS)))
    sleep_between = workers / RPS if RPS > 0 else 1.0

    print(
        f"loadgen: {TARGET_URL} at ~{RPS} req/s across {workers} connection(s), "
        f"{ERROR_RATIO:.0%} of requests hitting the fault endpoint"
    )
    if DURATION:
        print(f"loadgen: stopping after {DURATION:.0f}s")

    stats = Stats()
    clients = [
        Client(TARGET_URL, seed=None if SEED is None else int(SEED) + index)
        for index in range(workers)
    ]
    threads = [
        threading.Thread(target=worker, args=(client, stats, sleep_between), daemon=True)
        for client in clients
    ]

    started = time.monotonic()
    for thread in threads:
        thread.start()

    try:
        while not _stop.is_set():
            time.sleep(1)
            if DURATION and time.monotonic() - started >= DURATION:
                _stop.set()
    finally:
        for thread in threads:
            thread.join(timeout=5)
        for client in clients:
            client.close()

    elapsed = time.monotonic() - started
    print(f"loadgen: stopped after {elapsed:.1f}s -- {stats.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

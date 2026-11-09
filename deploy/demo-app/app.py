"""A tiny service that can be told to fail, for end-to-end triage demos.

Standard library only, so it runs from a stock python image with no build.

Modes (env MODE at start, or POST /mode/<mode> at runtime):
  healthy  ~0.5% errors
  errors   ~30% 500s; logs show a downstream connection being refused
  slow     high latency; logs show the request queue filling up
  leak     allocates memory until the container is OOM-killed
  crash    exits on start because a required setting is missing (crash loop)

It generates its own traffic, exposes Prometheus metrics on /metrics, logs to
stdout, and (if LOKI_URL is set) pushes the same log lines to Loki.
"""

from __future__ import annotations

import json
import os
import random
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("healthy", "errors", "slow", "leak", "crash")
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

state = {"mode": os.environ.get("MODE", "healthy")}
lock = threading.Lock()
counts: dict[str, int] = {"200": 0, "500": 0}
hist = [0] * (len(BUCKETS) + 1)
hist_sum = 0.0
leak: list[bytes] = []
pending_logs: list[tuple[int, str]] = []

LOKI_URL = os.environ.get("LOKI_URL", "")
LABELS = {
    "namespace": os.environ.get("POD_NAMESPACE", "demo"),
    "app": os.environ.get("APP_NAME", "demo-app"),
    "pod": os.environ.get("HOSTNAME", socket.gethostname()),
}


def log(level: str, message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {level} {message}"
    print(line, flush=True)
    if LOKI_URL:
        with lock:
            pending_logs.append((time.time_ns(), line))


def ship_logs() -> None:
    while True:
        time.sleep(2)
        with lock:
            batch = pending_logs[:]
            pending_logs.clear()
        if not batch:
            continue
        body = json.dumps(
            {"streams": [{"stream": LABELS, "values": [[str(ts), line] for ts, line in batch]}]}
        ).encode()
        req = urllib.request.Request(
            f"{LOKI_URL}/loki/api/v1/push", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=5).close()  # noqa: S310 - fixed internal URL
        except OSError as exc:
            print(f"loki push failed: {exc}", file=sys.stderr, flush=True)


def observe(code: str, seconds: float) -> None:
    global hist_sum
    with lock:
        counts[code] += 1
        hist_sum += seconds
        for i, bound in enumerate(BUCKETS):
            if seconds <= bound:
                hist[i] += 1
        hist[-1] += 1


def traffic() -> None:
    while True:
        mode = state["mode"]
        latency = random.uniform(0.01, 0.08)
        code = "200"
        if mode == "healthy" and random.random() < 0.005:
            code = "500"
            log("ERROR", "GET /api/items 500: transient error")
        elif mode == "errors" and random.random() < 0.3:
            code = "500"
            log(
                "ERROR",
                "POST /api/pay 500: upstream payment-gateway: dial tcp 10.0.4.7:8443: "
                "connect: connection refused",
            )
        elif mode == "slow":
            latency = random.uniform(0.8, 2.0)
            if random.random() < 0.1:
                log("WARN", "request queue full (depth=256), shedding load")
        elif mode == "leak":
            leak.append(b"x" * 1024 * 1024)
            if len(leak) % 10 == 0:
                log("INFO", f"cache size {len(leak)} MiB, evictions 0")
        observe(code, latency)
        time.sleep(0.1)


def rss_bytes() -> int:
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        return 0


def render_metrics() -> str:
    with lock:
        lines = [
            "# TYPE http_requests_total counter",
            *(f'http_requests_total{{code="{c}"}} {n}' for c, n in counts.items()),
            "# TYPE http_request_duration_seconds histogram",
            *(
                f'http_request_duration_seconds_bucket{{le="{b}"}} {hist[i]}'
                for i, b in enumerate(BUCKETS)
            ),
            f'http_request_duration_seconds_bucket{{le="+Inf"}} {hist[-1]}',
            f"http_request_duration_seconds_sum {hist_sum}",
            f"http_request_duration_seconds_count {hist[-1]}",
            "# TYPE demo_app_resident_memory_bytes gauge",
            f"demo_app_resident_memory_bytes {rss_bytes()}",
            "# TYPE demo_app_mode gauge",
            *(f'demo_app_mode{{mode="{m}"}} {int(m == state["mode"])}' for m in MODES),
        ]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype: str = "text/plain") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/metrics":
            self._send(200, render_metrics(), "text/plain; version=0.0.4")
        else:
            self._send(200, json.dumps({"mode": state["mode"]}), "application/json")

    def do_POST(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "mode" and parts[1] in MODES:
            state["mode"] = parts[1]
            log("INFO", f"mode changed to {parts[1]}")
            self._send(200, json.dumps({"mode": state["mode"]}), "application/json")
        else:
            self._send(404, f"use POST /mode/<{'|'.join(MODES)}>")

    def log_message(self, *args: object) -> None:  # keep stdout for app logs
        return


def main() -> None:
    if state["mode"] == "crash":
        log("INFO", "starting demo-app 1.4.0")
        log("ERROR", "config: required environment variable PAYMENT_GATEWAY_URL is not set")
        time.sleep(1)
        sys.exit(1)
    log("INFO", f"starting demo-app 1.4.0 mode={state['mode']}")
    for target in (traffic, ship_logs):
        threading.Thread(target=target, daemon=True).start()
    port = int(os.environ.get("PORT", "8081"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()

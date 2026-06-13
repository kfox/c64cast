#!/usr/bin/env python3
"""Async write-pipeline benchmark.

Spins up a local HTTP server that accepts (and discards) the U64 REST
write endpoints, points an ``Ultimate64API`` at it, and measures:

  * throughput in bytes/sec and requests/sec
  * latency p50 / p95 / max
  * delta-cache effectiveness (skip ratio)

This is the regression harness for the API layer — when you change
``write_region``'s diff strategy or the async queue, run this and
compare against the previous numbers. The fake server adds an optional
artificial per-request latency so you can simulate a slow LAN.

Run via ``make bench`` or ``python scripts/bench.py [--latency-ms N]``.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from c64cast.api import Ultimate64API  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    latency_ms = 0.0
    request_count = 0
    bytes_received = 0
    latencies_ms: list[float] = []
    _lock = threading.Lock()

    def _ack(self):
        t0 = time.perf_counter()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.latency_ms > 0:
            time.sleep(self.latency_ms / 1000.0)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        with _Handler._lock:
            _Handler.request_count += 1
            _Handler.bytes_received += len(body)
            _Handler.latencies_ms.append(dt_ms)

    # The U64 API uses PUT / POST / GET on a few endpoints. Accept them all.
    def do_PUT(self):  # noqa: N802
        self._ack()

    def do_POST(self):  # noqa: N802
        self._ack()

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass  # silence the per-request stderr noise


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    arr = np.asarray(xs, dtype=np.float64)
    return float(np.percentile(arr, p))


def _reset_stats():
    with _Handler._lock:
        _Handler.request_count = 0
        _Handler.bytes_received = 0
        _Handler.latencies_ms = []


def _print_section(title: str):
    print()
    print(title)
    print("-" * len(title))


def run_bench(latency_ms: float = 0.0, frames: int = 600,
              region_bytes: int = 8000) -> None:
    """Replay a few realistic write patterns against the local fake U64.

    * **full_writes** — every frame pushes a brand-new 8 KB region (worst
      case; simulates a fresh bitmap mode with no temporal coherence).
    * **delta_writes** — same region but only a small slice changes per
      frame (typical of a waveform/spectrum overlay over static text).
    * **mixed** — alternates: most frames are deltas, every ~30th is a
      full reset (simulates scene transitions).
    """
    _Handler.latency_ms = latency_ms
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="bench-http")
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    print(f"fake U64 listening on {base_url}  (per-request latency: "
          f"{latency_ms:.1f} ms)")
    print(f"frames={frames}, region_bytes={region_bytes}")

    rng = np.random.default_rng(0)
    region = rng.integers(0, 256, size=region_bytes, dtype=np.uint8)

    # ---- full_writes ------------------------------------------------------
    api = Ultimate64API(base_url, async_writes=True, queue_depth=32)
    api.invalidate_cache()
    _reset_stats()
    t0 = time.perf_counter()
    for _ in range(frames):
        # Fresh content every frame — region is completely different.
        region = rng.integers(0, 256, size=region_bytes, dtype=np.uint8)
        api.write_region(0x2000, region.tobytes(), region_id=99)
    api.flush(timeout=30.0)
    api.close()
    dt = time.perf_counter() - t0
    _print_section("full_writes (fresh region every frame)")
    _report(api, dt, frames, region_bytes)

    # ---- localized_delta --------------------------------------------------
    # A contiguous window changes each frame — mimics a marquee/clock/
    # scrolling-text overlay where only a few neighbouring cells differ.
    api = Ultimate64API(base_url, async_writes=True, queue_depth=32)
    api.invalidate_cache()
    region = rng.integers(0, 256, size=region_bytes, dtype=np.uint8)
    api.write_region(0x2000, region.tobytes(), region_id=99)
    _reset_stats()
    window = 40                          # 40 bytes = one PETSCII row
    t0 = time.perf_counter()
    for i in range(frames):
        start = (i * 7) % (region_bytes - window)
        region[start:start + window] = rng.integers(
            0, 256, size=window, dtype=np.uint8)
        api.write_region(0x2000, region.tobytes(), region_id=99)
    api.flush(timeout=30.0)
    api.close()
    dt = time.perf_counter() - t0
    _print_section(f"localized_delta ({window}-byte window per frame)")
    _report(api, dt, frames, region_bytes)

    # ---- chunked_delta ----------------------------------------------------
    # Sparse scattered changes — what a spectrum analyser looks like (8
    # bars updating across the row). Tests the chunked-diff path.
    api = Ultimate64API(base_url, async_writes=True, queue_depth=32)
    api.invalidate_cache()
    region = rng.integers(0, 256, size=region_bytes, dtype=np.uint8)
    api.write_region(0x2000, region.tobytes(), region_id=99)
    _reset_stats()
    t0 = time.perf_counter()
    for _ in range(frames):
        # 8 bands × 8 bytes each, spread across the region.
        for b in range(8):
            base = b * (region_bytes // 8)
            region[base:base + 8] = rng.integers(
                0, 256, size=8, dtype=np.uint8)
        api.write_region(0x2000, region.tobytes(), region_id=99)
    api.flush(timeout=30.0)
    api.close()
    dt = time.perf_counter() - t0
    _print_section("chunked_delta (8 scattered 8-byte bands per frame)")
    _report(api, dt, frames, region_bytes)

    # ---- no_op ------------------------------------------------------------
    # Region pushed unchanged every frame — must skip every send.
    api = Ultimate64API(base_url, async_writes=True, queue_depth=32)
    api.invalidate_cache()
    region = rng.integers(0, 256, size=region_bytes, dtype=np.uint8)
    api.write_region(0x2000, region.tobytes(), region_id=99)
    _reset_stats()
    t0 = time.perf_counter()
    for _ in range(frames):
        api.write_region(0x2000, region.tobytes(), region_id=99)
    api.flush(timeout=30.0)
    api.close()
    dt = time.perf_counter() - t0
    _print_section("no_op (identical region each frame — should skip)")
    _report(api, dt, frames, region_bytes)

    server.shutdown()
    server.server_close()


def _report(api: Ultimate64API, dt: float, frames: int, region_bytes: int):
    bytes_total = _Handler.bytes_received
    reqs = _Handler.request_count
    fps = frames / dt if dt > 0 else 0.0
    mbps = bytes_total / dt / 1e6 if dt > 0 else 0.0
    print(f"wall time          : {dt * 1000:.1f} ms ({fps:.1f} frames/s)")
    print(f"requests           : {reqs} ({reqs / dt:.0f} req/s)")
    print(f"bytes uploaded     : {bytes_total / 1024:.1f} KiB "
          f"({mbps:.2f} MB/s)")
    print(f"avg bytes/request  : {(bytes_total / reqs) if reqs else 0:.0f}")
    print(f"latency p50 / p95  : "
          f"{_percentile(_Handler.latencies_ms, 50):.2f} ms / "
          f"{_percentile(_Handler.latencies_ms, 95):.2f} ms")
    print(f"latency max        : {max(_Handler.latencies_ms or [0]):.2f} ms")
    print(f"api skipped frames : {api.stats['skipped']}")
    if frames * region_bytes > 0:
        compression = 1.0 - bytes_total / (frames * region_bytes)
        print(f"delta efficiency   : {compression * 100:.1f}% bytes saved "
              "vs. naive full upload")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--latency-ms", type=float, default=0.0,
                   help="Simulated per-request latency the fake server "
                        "adds before responding (default: 0 = pure local).")
    p.add_argument("--frames", type=int, default=600,
                   help="Frames to simulate per scenario.")
    p.add_argument("--region-bytes", type=int, default=8000,
                   help="Size of the simulated region (default: 8 KB, "
                        "matches the hires bitmap area).")
    args = p.parse_args()
    run_bench(latency_ms=args.latency_ms,
              frames=args.frames,
              region_bytes=args.region_bytes)
    return 0


if __name__ == "__main__":
    sys.exit(main())

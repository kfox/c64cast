#!/usr/bin/env python3
"""Measure R_rate (NMI consumer) across display modes to inform pitch compensation.

The host-DMA servo locks W (write pace) to R (NMI consumer), which runs at ~7690 B/s
in petscii due to video DMA bus-halts (the servo fixes the echo but trades constant
~4% slowdown). This script measures R_rate across three modes (petscii, mhires, blank)
to understand:
1. Per-mode R_rate (how much slowdown per mode?)
2. Whether a fixed latch bump can compensate all modes or needs adaptive servo
3. Whether blank (minimal video) shows full 8000 or also loses ~4%

Run from the c64cast project root:
    python scripts/diags/measure_r_rate_all_modes.py
    python scripts/diags/measure_r_rate_all_modes.py --url http://u2p.lan

Each mode runs for ~45s and writes a CSV. Summary printed at the end.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import _diaglib as d

from c64cast.audio import (
    NMI_ROUTINE_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_SIZE,
)

READ_PTR_ADDR = NMI_ROUTINE_ADDR + 5

# (config_path, mode_name)
# Test order: petscii (light), hires (moderate), mhires (heavy), repeated at different fps
MODES = [
    ("scripts/diags/out/r_rate_petscii_60fps.toml", "petscii@60fps"),
    ("scripts/diags/out/r_rate_petscii_30fps.toml", "petscii@30fps"),
    ("scripts/diags/out/r_rate_hires_60fps.toml", "hires@60fps"),
    ("scripts/diags/out/r_rate_hires_30fps.toml", "hires@30fps"),
    ("scripts/diags/out/r_rate_mhires_60fps.toml", "mhires@60fps"),
    ("scripts/diags/out/r_rate_mhires_30fps.toml", "mhires@30fps"),
]


def _read_r(url: str) -> int | None:
    """Read R as a ring address, or None if unreadable."""
    b = d.rest_readmem(READ_PTR_ADDR, 2, url)
    if not b or len(b) != 2:
        return None
    addr = b[0] | (b[1] << 8)
    return addr if RING_BUFFER_ADDR <= addr < RING_BUFFER_END else None


def _linfit(ts: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares slope and intercept."""
    n = len(ts)
    if n < 2:
        return 0.0, 0.0
    mt = sum(ts) / n
    my = sum(ys) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    sxy = sum((t - mt) * (y - my) for t, y in zip(ts, ys, strict=False))
    slope = sxy / sxx if sxx else 0.0
    return slope, my - slope * mt


def measure_mode(config_path: str, mode_name: str, url: str, poll_duration: float = 45.0) -> dict:
    """Measure R_rate for one mode. Returns dict with slope, intercept, stats."""
    cfg = Path(config_path)
    if not cfg.exists():
        return {"error": f"config not found: {cfg}"}

    print(f"\n{'=' * 70}")
    print(f"[{mode_name.upper()}] launching c64cast with {cfg}")
    print(f"{'=' * 70}")

    app = subprocess.Popen(
        [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", url],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for boot + first audio
    boot_wait = 10.0
    print(f"[boot] waiting {boot_wait}s for boot + first audio")
    time.sleep(boot_wait)

    # Poll R at 50 Hz
    period = 1.0 / 50.0
    rows: list[tuple[float, int, int]] = []
    missed = 0
    prev_r: int | None = None
    cum = 0

    print(f"[probe] polling R for {poll_duration}s @ 50 Hz")
    t0 = time.time()
    next_t = t0
    try:
        while time.time() - t0 < poll_duration:
            now = time.time()
            if now < next_t:
                time.sleep(next_t - now)
            next_t += period
            r = _read_r(url)
            if r is None:
                missed += 1
                continue
            if prev_r is not None:
                delta = (r - prev_r) % RING_BUFFER_SIZE
                if delta >= RING_BUFFER_SIZE // 2:
                    delta = 0
                cum += delta
            prev_r = r
            rows.append((round(now - t0, 4), r, cum))
    finally:
        app.terminate()
        try:
            app.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.kill()
        # Reset after each mode
        print("[reset] machine:reset")
        d.rest_reset(url)
        time.sleep(1)

    # Analysis
    if len(rows) < 10:
        return {"error": f"only {len(rows)} samples ({missed} missed)", "mode": mode_name}

    ts = [r[0] for r in rows]
    ys = [r[2] for r in rows]
    slope, intercept = _linfit(ts, ys)
    r_rate = slope  # bytes/second

    # Report
    result = {
        "mode": mode_name,
        "samples": len(rows),
        "missed": missed,
        "r_rate_bps": r_rate,
        "slowdown_pct": 100 * (8000 - r_rate) / 8000,
        "cumulative_bytes": ys[-1] if ys else 0,
        "poll_duration_s": poll_duration,
    }

    print(f"[result] {mode_name}")
    print(f"  R_rate = {r_rate:.1f} B/s")
    print(f"  slowdown = {result['slowdown_pct']:.1f}% (nominal 8000 B/s)")
    print(f"  samples = {len(rows)} (missed {missed})")

    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help="U64 URL")
    ap.add_argument(
        "--duration", type=float, default=30.0, help="poll duration per mode (default 30)"
    )
    ap.add_argument(
        "--no-reset", action="store_true", help="don't reset after measurements (for debugging)"
    )
    args = ap.parse_args()

    results: list[dict] = []
    try:
        for config_path, mode_name in MODES:
            result = measure_mode(config_path, mode_name, args.url, args.duration)
            results.append(result)
    except KeyboardInterrupt:
        print("\n[interrupted]")
        if not args.no_reset:
            d.rest_reset(args.url)
        return 1

    # Summary
    print(f"\n{'=' * 70}")
    print("[SUMMARY]")
    print(f"{'=' * 70}")

    valid_results = [r for r in results if "error" not in r]
    if not valid_results:
        print("No valid results")
        return 1

    print(f"\n{'Mode':<12} {'R_rate (B/s)':<15} {'Slowdown':<15}")
    print(f"{'-' * 42}")
    for r in valid_results:
        print(f"{r['mode']:<12} {r['r_rate_bps']:>8.1f}       {r['slowdown_pct']:>6.1f}%")

    # Analysis
    print("\n[analysis]")
    avg_slowdown = sum(r["slowdown_pct"] for r in valid_results) / len(valid_results)
    print(f"Average slowdown across modes: {avg_slowdown:.1f}%")

    max_slowdown = max(r["slowdown_pct"] for r in valid_results)
    min_slowdown = min(r["slowdown_pct"] for r in valid_results)
    print(f"Range: {min_slowdown:.1f}% (lightest) to {max_slowdown:.1f}% (heaviest)")
    print(f"Spread: {max_slowdown - min_slowdown:.1f}% (fixed bump OK if < 0.5%)")

    # Recommendation
    if max_slowdown - min_slowdown < 0.5:
        print(
            f"\n[recommendation] Fixed NMI latch bump by ~{avg_slowdown:.1f}% "
            f"should work for all modes."
        )
    else:
        print(
            f"\n[recommendation] Mode-dependent slowdown is {max_slowdown - min_slowdown:.1f}% "
            f"(too wide for fixed bump). Adaptive servo required."
        )

    if not args.no_reset:
        print("\n[final reset]")
        d.rest_reset(args.url)

    return 0


if __name__ == "__main__":
    sys.exit(main())

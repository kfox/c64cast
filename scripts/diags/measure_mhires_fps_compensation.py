#!/usr/bin/env python3
"""Measure how target_fps affects R_rate in mhires commercial playback.

Focused measurement: the main use case is commercial playback in mhires. This script
tests how varying target_fps (which controls DMA write frequency) affects the R_rate
compensation needed. If 30 fps dramatically improves R_rate compared to 60 fps, then
we could suggest running commercials at 30 fps as a simple pitch fix.

Run from c64cast project root:
    python scripts/diags/measure_mhires_fps_compensation.py
    python scripts/diags/measure_mhires_fps_compensation.py --url http://u2p.lan
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

MODES = [
    ("scripts/diags/out/r_rate_commercial_mhires_60fps.toml", "mhires@60fps"),
    ("scripts/diags/out/r_rate_commercial_mhires_30fps.toml", "mhires@30fps"),
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
    """Measure R_rate for one mode."""
    cfg = Path(config_path)
    if not cfg.exists():
        return {"error": f"config not found: {cfg}"}

    print(f"\n{'='*70}")
    print(f"[{mode_name.upper()}] launching {cfg.name}")
    print(f"{'='*70}")

    app = subprocess.Popen([d.python_exe(), "-m", "c64cast",
                            "--config", str(cfg), "--url", url],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    boot_wait = 12.0  # commercial takes longer to start
    print(f"[boot] waiting {boot_wait}s for boot + video playback")
    time.sleep(boot_wait)

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
        print("[reset] machine:reset")
        d.rest_reset(url)
        time.sleep(1)

    if len(rows) < 10:
        return {"error": f"only {len(rows)} samples ({missed} missed)", "mode": mode_name}

    ts = [r[0] for r in rows]
    ys = [r[2] for r in rows]
    slope, _ = _linfit(ts, ys)
    r_rate = slope

    result = {
        "mode": mode_name,
        "samples": len(rows),
        "r_rate_bps": r_rate,
        "slowdown_pct": 100 * (8000 - r_rate) / 8000,
        "nmi_latch_compensation_pct": (8000 - r_rate) / r_rate * 100 if r_rate > 0 else 0,
    }

    print(f"[result] {mode_name}")
    print(f"  R_rate = {r_rate:.1f} B/s")
    print(f"  slowdown = {result['slowdown_pct']:.2f}%")
    print(f"  latch bump needed = +{result['nmi_latch_compensation_pct']:.2f}%")
    print(f"  samples = {len(rows)} (missed {missed})")

    return result

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help="U64 URL")
    ap.add_argument("--duration", type=float, default=45.0,
                    help="poll duration per mode (default 45)")
    args = ap.parse_args()

    results: list[dict] = []
    try:
        for config_path, mode_name in MODES:
            result = measure_mode(config_path, mode_name, args.url, args.duration)
            results.append(result)
    except KeyboardInterrupt:
        print("\n[interrupted]")
        d.rest_reset(args.url)
        return 1

    # Summary
    print(f"\n{'='*70}")
    print("[SUMMARY]")
    print(f"{'='*70}")

    valid_results = [r for r in results if "error" not in r]
    if not valid_results:
        print("No valid results")
        return 1

    print(f"\n{'Mode':<15} {'R_rate (B/s)':<15} {'Slowdown':<12} {'Latch bump':<12}")
    print(f"{'-'*55}")
    for r in valid_results:
        print(f"{r['mode']:<15} {r['r_rate_bps']:>8.1f}       "
              f"{r['slowdown_pct']:>6.2f}%     {r['nmi_latch_compensation_pct']:>+6.2f}%")

    # Analysis
    print("\n[ANALYSIS]")
    if len(valid_results) >= 2:
        r60 = next((r for r in valid_results if "60fps" in r["mode"]), None)
        r30 = next((r for r in valid_results if "30fps" in r["mode"]), None)
        if r60 and r30:
            improvement = r60["slowdown_pct"] - r30["slowdown_pct"]
            print("FPS impact on mhires compensation:")
            print(f"  60 fps: {r60['slowdown_pct']:.2f}% slowdown → latch +{r60['nmi_latch_compensation_pct']:.2f}%")
            print(f"  30 fps: {r30['slowdown_pct']:.2f}% slowdown → latch +{r30['nmi_latch_compensation_pct']:.2f}%")
            print(f"  improvement: {improvement:.2f}% (60→30 fps)")
            if improvement > 2.0:
                print("\n[FINDING] Running mhires at 30 fps gives significant pitch improvement.")
                print("         Consider: a) set target_fps=30 for commercial scenes")
                print(f"                 OR b) adaptive latch bump to compensate ~{valid_results[0]['nmi_latch_compensation_pct']:.2f}%")
            else:
                print("\n[FINDING] FPS has minimal effect. Need adaptive latch compensation.")

    print("\n[final reset]")
    d.rest_reset(args.url)
    return 0

if __name__ == "__main__":
    sys.exit(main())

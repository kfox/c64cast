#!/usr/bin/env python3
"""Measure the host-DMA audio drift (echo root cause) without touching audio.py.

The host-DMA worker paces its ring WRITES strictly to wall-clock: each chunk is
chunk_size bytes shipped every chunk_size/sample_rate seconds, so the write
pointer W advances at *exactly* sample_rate bytes/sec, locked to the monotonic
clock (audio.py `_worker`, "strict absolute" pacing — no snap-forward). W lives
only in Python (`write_addr`), so it can't be read over REST; but we don't need
it. The DRIFT that causes the echo is:

    drift = W_rate (= sample_rate, software-exact) - R_rate (the NMI consumer)

and R — the NMI DAC read pointer — IS externally readable: it's the self-
modifying LDA operand at $C025 (LO) / $C026 (HI) in NMI_ROUTINE. The NMI
consumer should also run at ~sample_rate, but it loses ticks to VIC-II video
DMA bus-halts (edge-triggered NMI: multiple timer underflows during a halt
collapse to one pending NMI -> reads lost), so R_rate < sample_rate. The gap is
the producer-over-consumer surplus that laps the 8 KB ring and is heard as echo.

This probe polls R at a steady rate, unwraps it across ring laps into a
monotonic cumulative byte count, linear-fits the slope = R_rate, and reports the
drift and predicted lap (echo) period. Pure read-only REST; coexists with
c64cast's DMA socket; perturbs nothing.

    scripts/diags/hostdma_drift_probe.py --config scripts/diags/out/hostdma_pinned.toml -t 40
    scripts/diags/hostdma_drift_probe.py --attach -t 30        # c64cast already up

Writes a per-sample CSV under scripts/diags/out/ and prints a summary.
Resets the machine on exit unless --no-reset / --attach.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

import _diaglib as d

from c64cast.audio import (
    NMI_ROUTINE_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_SIZE,
)

# Read-pointer operand: NMI_ROUTINE offset 5/6 -> LDA $00?? operand = $C025/$C026.
READ_PTR_ADDR = NMI_ROUTINE_ADDR + 5          # $C025 (LO), $C026 (HI)


def _read_r(url: str) -> int | None:
    """Read R (NMI read pointer) as a ring address, or None if unreadable/insane."""
    b = d.rest_readmem(READ_PTR_ADDR, 2, url)
    if not b or len(b) != 2:
        return None
    addr = b[0] | (b[1] << 8)
    return addr if RING_BUFFER_ADDR <= addr < RING_BUFFER_END else None


def _linfit(ts: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares slope, intercept of ys = slope*ts + b."""
    n = len(ts)
    mt = sum(ts) / n
    my = sum(ys) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    sxy = sum((t - mt) * (y - my) for t, y in zip(ts, ys, strict=False))
    slope = sxy / sxx if sxx else 0.0
    return slope, my - slope * mt


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--config", help="c64cast TOML (host-DMA audio) to launch")
    g.add_argument("--attach", action="store_true",
                   help="probe an already-running c64cast (don't launch/reset)")
    ap.add_argument("-t", "--seconds", type=float, default=40.0,
                    help="poll duration after boot (default 40)")
    ap.add_argument("--hz", type=float, default=50.0, help="poll rate (default 50)")
    ap.add_argument("--boot", type=float, default=8.0,
                    help="seconds to wait for boot + first audio (default 8)")
    ap.add_argument("--sample-rate", type=int, default=8000,
                    help="nominal W rate = audio sample_rate (default 8000)")
    ap.add_argument("--label", default="hostdma_drift")
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()

    app: subprocess.Popen[bytes] | None = None
    if args.config:
        cfg = Path(args.config)
        if not cfg.exists():
            ap.error(f"config not found: {cfg}")
        print(f"[run] python -m c64cast --config {cfg}")
        app = subprocess.Popen([d.python_exe(), "-m", "c64cast",
                                "--config", str(cfg), "--url", args.url])
        print(f"[boot] waiting {args.boot:g}s for boot + first audio")
        time.sleep(args.boot)

    period = 1.0 / args.hz
    csv_path = d.stamped(args.label, "csv")
    # rows: t, R_addr, cumulative_bytes_consumed
    rows: list[tuple[float, int, int]] = []
    missed = 0
    prev_r: int | None = None
    cum = 0

    print(f"[probe] polling R($C025/26) for {args.seconds:g}s @ {args.hz:g}Hz")
    print(f"        ring=${RING_BUFFER_ADDR:04X}-${RING_BUFFER_END - 1:04X} "
          f"size={RING_BUFFER_SIZE}  nominal W={args.sample_rate} B/s")
    t0 = time.time()
    next_t = t0
    try:
        while time.time() - t0 < args.seconds:
            now = time.time()
            if now < next_t:
                time.sleep(next_t - now)
            next_t += period
            r = _read_r(args.url)
            if r is None:
                missed += 1
                continue
            if prev_r is not None:
                delta = (r - prev_r) % RING_BUFFER_SIZE  # forward advance, unwrap laps
                # Guard against a missed-poll super-jump being mis-unwrapped: at
                # 50 Hz and ~8000 B/s, ~160 B/poll; a real step is well under a
                # ring. Anything >= ring/2 is a backward blip (read tear) -> 0.
                if delta >= RING_BUFFER_SIZE // 2:
                    delta = 0
                cum += delta
            prev_r = r
            rows.append((round(now - t0, 4), r, cum))
    finally:
        if app is not None:
            app.terminate()
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.kill()
        if not args.no_reset and not args.attach:
            print(f"[reset] machine:reset -> {d.rest_reset(args.url)}")

    if len(rows) < 10:
        print(f"[FAIL] only {len(rows)} samples ({missed} missed). "
              "Is audio actually playing?")
        return 1

    with csv_path.open("w") as f:
        f.write("t_s,R_addr,cum_bytes\n")
        for t, r, c in rows:
            f.write(f"{t},{r},{c}\n")

    ts = [r[0] for r in rows]
    cums = [float(r[2]) for r in rows]
    r_rate, _ = _linfit(ts, cums)
    span = ts[-1] - ts[0]
    w_rate = float(args.sample_rate)
    drift = w_rate - r_rate
    print(f"\n[probe] {len(rows)} samples, {missed} missed, span={span:.1f}s")
    print(f"  R_rate (NMI consumer, measured) = {r_rate:8.1f} B/s")
    print(f"  W_rate (host-DMA producer, nom) = {w_rate:8.1f} B/s")
    print(f"  drift (W - R)                   = {drift:+8.1f} B/s "
          f"({'W faster -> echo' if drift > 0 else 'R faster -> underrun'})")
    if abs(drift) > 1e-6:
        lap_s = RING_BUFFER_SIZE / abs(drift)
        print(f"  predicted lap (echo) period     = {lap_s:8.1f} s "
              f"(ring {RING_BUFFER_SIZE} B / |drift|)")
    print(f"  csv -> {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

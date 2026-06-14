#!/usr/bin/env python3
"""De-risk the REU audio-pump rate servo, externally, before touching audio.py.

reu_margin_probe.py showed the pump (W) out-produces the NMI reader (R) by a
steady, content-dependent 350-570 B/s (R loses ticks to video DMA bus-halts),
so the 8 KB ring drifts and laps every ~15-23s = echo. The fix is to match the
pump rate to the *actual* consumer rate by trimming the CIA #1 Timer A latch
($DD04/$DD05). This tool proves that control loop works from the host side —
reading R via REST readmem and writing the latch via REST writemem, both
independent of c64cast's DMA socket — so the eventual in-process servo is a
known quantity.

Modes:
  --measure          report R/W rates + drift at the current latch
  --sweep A,B,C      set each CIA#1 latch value, measure resulting W & R rates
                     (the actuator transfer function: bytes/s per latch unit)
  --servo            closed loop: feed-forward latch = 128*clock/R_rate - 1
                     plus an integral phase trim toward half-ring; hold and
                     report whether phase stays locked (no near-laps)

    scripts/diags/reu_servo_probe.py --config scripts/diags/reu_audio_plain.toml --sweep 16383,20480,24576
    scripts/diags/reu_servo_probe.py --config scripts/diags/reu_audio_plain.toml --servo -t 40

Launches c64cast (--config) or attaches (--attach). Restores the nominal
latch and resets the machine on exit (unless --attach/--no-reset).
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import time
from pathlib import Path

import _diaglib as d

from c64cast.audio import (
    NMI_ROUTINE_ADDR,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_PUMP_CIA1_LATCH,
    REU_PUMP_INITIAL_MARGIN,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_SIZE,
)

READ_PTR_ADDR = NMI_ROUTINE_ADDR + 5  # $C025 (LO) / $C026 (HI) — NMI read ptr R
REU_DST_REG_ADDR = 0xDF02  # REU dst reg (plain-path W fallback)
CIA1_TIMER_A_LO = 0xDD04  # CIA #1 Timer A latch LO/HI (pump rate)
CLOCK_NTSC = 1022727
CLOCK_PAL = 985248
CHUNK = 128  # bytes per pump IRQ (REU_PUMP_CHUNK_SIZE)
TARGET_PHASE = REU_PUMP_INITIAL_MARGIN  # half ring


def _u16(b: bytes | None) -> int | None:
    return (b[0] | (b[1] << 8)) if b and len(b) == 2 else None


def _in_ring(a: int | None) -> bool:
    return a is not None and RING_BUFFER_ADDR <= a < RING_BUFFER_END


def _read_R(url: str) -> int | None:
    return _u16(d.rest_readmem(READ_PTR_ADDR, 2, url))


def _read_W(url: str) -> int | None:
    trk = d.rest_readmem(REU_AUDIO_SRC_TRACKER_ADDR, 5, url)
    if trk and len(trk) == 5:
        cand = trk[3] | (trk[4] << 8)
        if _in_ring(cand):
            return cand
    cand = _u16(d.rest_readmem(REU_DST_REG_ADDR, 2, url))
    return cand if _in_ring(cand) else None


def _set_latch(url: str, latch: int) -> None:
    latch &= 0xFFFF
    d_requests_put(url, CIA1_TIMER_A_LO, f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}")


def d_requests_put(url: str, addr: int, data_hex: str) -> None:
    import requests

    requests.put(
        url + "/v1/machine:writemem", params={"address": f"{addr:04X}", "data": data_hex}, timeout=3
    )


def _rate_window(url: str, secs: float, hz: float) -> tuple[float, float, list[int]]:
    """Sample R & W over `secs`; return (R_rate, W_rate, phases) in bytes/s."""
    period = 1.0 / hz
    ts: list[float] = []
    Rs: list[int] = []
    Ws: list[int] = []
    t0 = time.time()
    nxt = t0
    while time.time() - t0 < secs:
        now = time.time()
        if now < nxt:
            time.sleep(nxt - now)
        nxt += period
        r, w = _read_R(url), _read_W(url)
        if r is None or w is None:
            continue
        ts.append(now - t0)
        Rs.append(r)
        Ws.append(w)
    if len(ts) < 3:
        return 0.0, 0.0, []

    def unwrap(v: list[int]) -> list[int]:
        out = [v[0]]
        for i in range(1, len(v)):
            dd = v[i] - v[i - 1]
            if dd < -RING_BUFFER_SIZE // 2:
                dd += RING_BUFFER_SIZE
            elif dd > RING_BUFFER_SIZE // 2:
                dd -= RING_BUFFER_SIZE
            out.append(out[-1] + dd)
        return out

    def slope(x: list[float], y: list[int]) -> float:
        n = len(x)
        mx, my = sum(x) / n, sum(y) / n
        den = sum((x[i] - mx) ** 2 for i in range(n))
        return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / den if den else 0.0

    phases = [(Ws[i] - Rs[i]) % RING_BUFFER_SIZE for i in range(len(ts))]
    return slope(ts, unwrap(Rs)), slope(ts, unwrap(Ws)), phases


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--config")
    src.add_argument("--attach", action="store_true")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--measure", action="store_true")
    mode.add_argument("--sweep", help="comma list of CIA#1 latch values to test")
    mode.add_argument("--servo", action="store_true")
    ap.add_argument("-t", "--seconds", type=float, default=40.0)
    ap.add_argument("--hz", type=float, default=25.0)
    ap.add_argument("--boot", type=float, default=7.0)
    ap.add_argument("--system", choices=["NTSC", "PAL"], default="NTSC")
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()
    clock = CLOCK_NTSC if args.system == "NTSC" else CLOCK_PAL

    app: subprocess.Popen[bytes] | None = None
    if args.config:
        cfg = Path(args.config)
        if not cfg.exists():
            ap.error(f"config not found: {cfg}")
        print(f"[run] python -m c64cast --config {cfg}")
        app = subprocess.Popen(
            [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", args.url]
        )
        print(f"[boot] waiting {args.boot:g}s")
        time.sleep(args.boot)

    rc = 0
    try:
        if args.measure:
            rr, wr, ph = _rate_window(args.url, args.seconds, args.hz)
            drift = wr - rr
            print(
                f"[measure] R={rr:.0f} W={wr:.0f} drift={drift:+.0f} B/s "
                f"(laps every {RING_BUFFER_SIZE / abs(drift):.1f}s)"
                if drift
                else ""
            )
            if ph:
                print(f"  phase median={int(statistics.median(ph))} min={min(ph)} max={max(ph)}")
        elif args.sweep:
            latches = [int(x) for x in args.sweep.split(",")]
            print(f"[sweep] nominal latch=${REU_PUMP_CIA1_LATCH:04X}; measuring W & R per latch")
            results = []
            for L in latches:
                _set_latch(args.url, L)
                time.sleep(1.5)  # settle
                rr, wr, ph = _rate_window(args.url, max(8.0, args.seconds / len(latches)), args.hz)
                results.append((L, rr, wr))
                print(
                    f"  latch=${L:04X} ({L:5d}): R={rr:.0f} W={wr:.0f} "
                    f"drift={wr - rr:+.0f} B/s  W_theory={CHUNK * clock / (L + 1):.0f}"
                )
            # actuator gain (B/s per latch unit) from first/last
            if len(results) >= 2:
                (L0, _, W0), (L1, _, W1) = results[0], results[-1]
                if L1 != L0:
                    g = (W1 - W0) / (L1 - L0)
                    print(f"  actuator gain ≈ {g:.3f} (W B/s) per latch unit")
                # null-drift latch for the *measured* consumer rate (avg R)
                avgR = statistics.mean(r for _, r, _ in results if r)
                null = round(CHUNK * clock / avgR) - 1
                print(
                    f"  avg consumer R={avgR:.0f} → null-drift latch ≈ "
                    f"${null:04X} ({null}) vs nominal ${REU_PUMP_CIA1_LATCH:04X}"
                )
        elif args.servo:
            rc = _run_servo(args, clock)
    finally:
        _set_latch(args.url, REU_PUMP_CIA1_LATCH)  # restore nominal before teardown
        if app is not None:
            app.terminate()
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.kill()
        if not args.no_reset and not args.attach:
            print(f"[reset] machine:reset -> {d.rest_reset(args.url)}")
    return rc


def _run_servo(args: argparse.Namespace, clock: int) -> int:
    """Closed loop: feed-forward latch from measured R rate + integral phase trim."""
    url = args.url
    dt = 0.8  # control period
    ki = 0.04  # integral gain on phase error (latch units / byte)
    latch = REU_PUMP_CIA1_LATCH
    lo, hi = 8000, 40000  # latch clamp (sane pump rate bounds)
    # rate estimator state
    prev_r = _read_R(url)
    prev_t = time.time()
    phases: list[int] = []
    near = 0
    print(f"[servo] target_phase={TARGET_PHASE}, dt={dt}s, ki={ki}")
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        time.sleep(dt)
        r, w = _read_R(url), _read_W(url)
        now = time.time()
        if r is None or w is None or prev_r is None:
            prev_r, prev_t = r, now
            continue
        # consumer rate from R delta (unwrap one step)
        dr = (r - prev_r) % RING_BUFFER_SIZE
        r_rate = dr / (now - prev_t)
        prev_r, prev_t = r, now
        phase = (w - r) % RING_BUFFER_SIZE
        phases.append(phase)
        nearest = min(phase, RING_BUFFER_SIZE - phase)
        if nearest < 512:
            near += 1
        # signed phase error toward half-ring
        err = phase - TARGET_PHASE
        if err > RING_BUFFER_SIZE // 2:
            err -= RING_BUFFER_SIZE
        elif err < -RING_BUFFER_SIZE // 2:
            err += RING_BUFFER_SIZE
        # feed-forward: latch matching consumer rate; +integral trim.
        # phase too big (W ahead) → pump too fast → INCREASE latch (slow pump).
        ff = (CHUNK * clock / r_rate - 1) if r_rate > 1000 else latch
        latch = int(max(lo, min(hi, ff + ki * err)))
        _set_latch(url, latch)
        print(
            f"  t={now - t0:5.1f} R={r_rate:.0f} phase={phase:5d} err={err:+5d} latch=${latch:04X}"
        )
    if phases:
        print(
            f"\n[servo] phase: median={int(statistics.median(phases))} "
            f"min={min(phases)} max={max(phases)} "
            f"stdev={statistics.pstdev(phases):.0f}"
        )
        print(
            f"  near-lap (<512B) events: {near}/{len(phases)} "
            f"→ {'LOCKED ✓' if near == 0 else 'still lapping ✗'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

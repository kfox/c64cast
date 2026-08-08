#!/usr/bin/env python3
"""Measure what a host DMA write costs the 6510, in NMI ticks, from the C64's side.

    scripts/diags/halt_shape_probe.py --url tr://
    scripts/diags/halt_shape_probe.py --url u64://192.168.2.64
    scripts/diags/halt_shape_probe.py --url tr:// --hold 120   # stationarity
    scripts/diags/halt_shape_probe.py --url tr:// --load-only 40 --write-rate 91
        # load generator only: occupy one link's bus while the OTHER backend
        # plays audio, to test whether a link's bus activity is what reaches
        # the SID. Measures nothing and leaves the machine alone.

Every halt figure in this tree is an inference from the C64-side REC DMA model
(1 cycle/byte) or a read off the capture rig. Neither says what a *host*
``DMAWRITE`` costs, and neither can compare the two backends' halt mechanisms —
the Ultimate's internal DMA engine versus the TeensyROM's cartridge-port
DMA/BA handshake — which is the one place the two paths still differ once ring
integrity, delivery, servo control and video contention are all measured clean.

The C64 can answer this itself. The NMI consumer's read pointer R advances once
per serviced NMI, so dR/dt *is* a tick counter, and ``ring_race_probe`` phase r
established that it reads back faithfully on both backends (fits nominal to
0.01%, zero backward steps). CIA #2 is edge-triggered, so a halt spanning
several Timer A underflows latches once and the rest collapse into it: the
deficit in dR/dt under load, against the same measurement with the bus quiet, is
the halt itself, counted in lost samples by the machine that lost them.

Sweeping payload size at a **fixed write rate** separates the halt into its two
terms:

    lost_ticks_per_write  =  a  +  b * payload_bytes

``b`` is the per-byte transfer cost — 1 cycle/byte predicts b = 1/85 ticks/byte
at the 12 kHz NTSC default. ``a`` is the fixed per-write cost: bus acquisition,
arbitration, stop/resume, everything that does not scale with the payload. A
backend with a larger ``a`` steals more cycles for the same delivered bytes, and
at the worker's ~91 writes/s each extra tick of ``a`` is another 0.76% of the
sample stream held rather than updated. That is the quantity this probe exists
to compare, and it is a pure memory measurement: no capture, no ears, no
avfoundation.

Fixed write rate rather than matched byte rate because it makes ``a`` and ``b``
a straight two-parameter fit against payload, and because holding bytes constant
instead drives the small payloads past the ~200 writes/s socket-DMA ceiling,
where the link, not the halt, would set the result. ``--byte-rate`` switches to
the matched-byte variant as a cross-check when the payloads stay under that
ceiling.

The ring is static and no feeder runs: this measures the cost of a write, not
playback, so nothing here can underrun. R is sampled at the same rate in every
condition including the baseline — a read is itself a DMA on the TeensyROM,
where it shares the write link, so it has to be present in the reference or its
cost would land in ``a``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import _diaglib as d

# Same bring-up as the race probe, imported rather than restated: a second copy
# of the reset/clear-loop/handler-upload sequence is how the two tools would end
# up measuring subtly different machines.
from ring_race_probe import arm, disarm, effective_rate, latch_for, read_r, setup

from c64cast.audio_handlers import RING_BUFFER_SIZE
from c64cast.config import Config
from c64cast.connect import apply_to_config, parse_connection_uri
from c64cast.hw.backend import make_backend

SCRATCH_ADDR = 0x6000  # clear of the ring ($4000-$5FFF) and the NMI handler ($C020)
DEFAULT_PAYLOADS = (64, 128, 256, 512, 1024)


def _fit_line(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least-squares ``y = a + b*x``, returning ``(a, b, r2)``."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys, strict=True))
    den = n * sxx - sx * sx
    if den == 0:
        return sy / n, 0.0, 0.0
    b = (n * sxy - sx * sy) / den
    a = (sy - b * sx) / n
    ybar = sy / n
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return a, b, r2


def measure(
    be,
    secs: float,
    *,
    r_rate: float,
    eff: float,
    payload: int,
    write_rate: float,
    addr: int,
) -> dict:
    """Fit dR/dt while issuing ``write_rate`` background writes/sec of ``payload``
    bytes. ``write_rate = 0`` is the quiet-bus reference.

    Writes and R samples are interleaved on one thread rather than run
    concurrently: the point of comparison is the halt, and a second thread would
    add backend-lock contention that differs between the two links and would be
    indistinguishable from it in the result.
    """
    data = bytes([0x5A]) * payload  # payload entropy is irrelevant — neither link
    # escapes, compresses or RLEs the segment body
    tag = f"{addr:04X}"
    w_period = 1.0 / write_rate if write_rate > 0 else float("inf")
    r_period = 1.0 / r_rate

    t0 = time.monotonic()
    next_w = t0 + w_period
    next_r = t0
    prev_r: int | None = None
    prev_t = 0.0
    good_bytes = 0.0
    good_secs = 0.0
    backward = 0
    failed = 0
    writes = 0
    expected_step = eff * r_period

    while True:
        now = time.monotonic()
        if now - t0 >= secs:
            break
        wake = min(next_w, next_r)
        if now < wake:
            time.sleep(wake - now)

        if time.monotonic() >= next_w:
            be.write_memory_file(tag, data)
            writes += 1
            next_w += w_period

        if time.monotonic() < next_r:
            continue
        next_r += r_period
        t = time.monotonic()
        r = read_r(be)
        if r is None:
            failed += 1
            continue
        if prev_r is not None:
            step = (r - prev_r) % RING_BUFFER_SIZE
            # Same guard as phase r: a stale read reappears mod-ring as a
            # near-full-lap jump, which at this sample rate cannot be real.
            if step > 2 * expected_step:
                backward += 1
            else:
                good_bytes += step
                good_secs += t - prev_t
        prev_r, prev_t = r, t

    elapsed = time.monotonic() - t0
    fitted = good_bytes / good_secs if good_secs > 0 else 0.0
    return {
        "payload": payload,
        "write_rate_req": write_rate,
        "write_rate_actual": writes / elapsed if elapsed > 0 else 0.0,
        "writes": writes,
        "byte_rate": writes * payload / elapsed if elapsed > 0 else 0.0,
        "fitted_rate_hz": fitted,
        "backward_steps": backward,
        "failed_reads": failed,
        "secs": elapsed,
    }


def load_only(be, secs: float, *, payload: int, write_rate: float, addr: int) -> int:
    """Occupy the bus with paced writes and do nothing else.

    The load generator for the cross-link experiment: the *other* backend plays
    audio through the production pipeline while this one writes scratch RAM at
    the worker's real rate. Every other mode in this file owns the machine —
    reset, clear loop, handler upload, arm, and a reset on the way out. This one
    must touch none of that, because a live ``c64cast`` is depending on all of
    it, so it skips the bring-up and the teardown rather than reusing
    ``measure``. It also takes no R samples: R belongs to the running app's
    consumer here, and reading it would add this link's own DMA to the very
    thing under test.
    """
    data = bytes([0x5A]) * payload
    tag = f"{addr:04X}"
    period = 1.0 / write_rate
    print(
        f"\n=== load only: {payload} B x {write_rate:g}/s to ${addr:04X} for {secs:g}s ===\n"
        "  no reset, no handler upload, no arm, no R reads, no teardown reset"
    )
    t0 = time.monotonic()
    nxt = t0
    writes = failed = 0
    while time.monotonic() - t0 < secs:
        now = time.monotonic()
        if now < nxt:
            time.sleep(nxt - now)
        nxt += period
        try:
            be.write_memory_file(tag, data)
            writes += 1
        except Exception as exc:  # a link error must not abandon the audio run
            failed += 1
            if failed <= 3:
                print(f"  [write failed] {exc}")
    el = time.monotonic() - t0
    print(
        f"  {writes} writes in {el:.1f}s = {writes / el:.1f}/s, "
        f"{writes * payload / el / 1024:.1f} KiB/s, {failed} failed"
    )
    be.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help="connection target (u64://…, tr://…)")
    ap.add_argument("--system", default="NTSC", choices=["NTSC", "PAL"])
    ap.add_argument("--nmi-rate", type=int, default=12000)
    ap.add_argument("-t", "--secs", type=float, default=20.0, help="seconds per condition")
    ap.add_argument("--r-rate", type=float, default=5.0, help="R samples/sec, every condition")
    ap.add_argument("--write-rate", type=float, default=48.0, help="background writes/sec")
    ap.add_argument(
        "--payloads",
        default=",".join(str(p) for p in DEFAULT_PAYLOADS),
        help="comma-separated payload sizes to sweep",
    )
    ap.add_argument(
        "--byte-rate",
        type=float,
        default=0.0,
        help="cross-check mode: hold total B/s here and vary the write rate instead",
    )
    ap.add_argument("--addr", type=lambda s: int(s, 16), default=SCRATCH_ADDR)
    ap.add_argument(
        "--hold",
        type=float,
        default=0.0,
        help="stationarity mode: one condition for this many seconds, reported in "
        "windows, to test whether the per-write cost drifts over a run",
    )
    ap.add_argument("--hold-payload", type=int, default=128)
    ap.add_argument("--window", type=float, default=10.0, help="--hold window length")
    ap.add_argument(
        "--load-only",
        type=float,
        default=0.0,
        help="load-generator mode: write --hold-payload bytes at --write-rate for this "
        "many seconds and measure nothing, leaving the machine untouched otherwise, so "
        "the other backend can be running a real playback at the same time",
    )
    args = ap.parse_args()

    eff = effective_rate(args.nmi_rate, args.system)
    cycles_per_tick = latch_for(args.nmi_rate, args.system) + 1
    payloads = [int(p) for p in args.payloads.split(",") if p.strip()]

    print(f"[setup] {args.url}  NMI {args.nmi_rate} -> effective {eff:.1f} Hz")
    print(
        f"[setup] {cycles_per_tick} cycles/tick; scratch ${args.addr:04X}; {args.secs:g}s/condition"
    )

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)

    if args.load_only > 0:
        return load_only(
            be,
            args.load_only,
            payload=args.hold_payload,
            write_rate=args.write_rate,
            addr=args.addr,
        )

    result: dict = {
        "url": args.url,
        "nmi_rate": args.nmi_rate,
        "effective_rate": eff,
        "cycles_per_tick": cycles_per_tick,
    }

    try:
        setup(be, args.system)
        arm(be, args.nmi_rate, args.system)
        time.sleep(1.0)
        if read_r(be) is None:
            print("[abort] R unreadable — this backend cannot report the read pointer")
            return 1

        print(f"\n=== reference: bus quiet ({args.secs:g}s, R reads only) ===")
        ref = measure(
            be, args.secs, r_rate=args.r_rate, eff=eff, payload=0, write_rate=0.0, addr=args.addr
        )
        base = ref["fitted_rate_hz"]
        result["reference"] = ref
        print(
            f"  dR/dt {base:.1f} B/s vs nominal {eff:.1f} "
            f"({(base - eff) / eff * 100:+.2f}%)  backward {ref['backward_steps']}  "
            f"failed {ref['failed_reads']}"
        )
        if base <= 0:
            print("[abort] reference measured no R advance — consumer not running")
            return 1

        if args.hold > 0:
            print(
                f"\n=== hold: {args.hold_payload} B x {args.write_rate:g}/s for {args.hold:g}s ==="
            )
            hdr = f"{'window':>12} {'dR/dt':>9} {'loss%':>7} {'ticks/wr':>9} {'wr/s':>6}"
            print(hdr)
            print("-" * len(hdr))
            windows = []
            t_start = time.monotonic()
            while time.monotonic() - t_start < args.hold:
                m = measure(
                    be,
                    args.window,
                    r_rate=args.r_rate,
                    eff=eff,
                    payload=args.hold_payload,
                    write_rate=args.write_rate,
                    addr=args.addr,
                )
                el = time.monotonic() - t_start
                lost = base - m["fitted_rate_hz"]
                per_w = lost / m["write_rate_actual"] if m["write_rate_actual"] > 0 else 0.0
                m["t_end"] = el
                m["lost_ticks_per_write"] = per_w
                windows.append(m)
                print(
                    f"{el:9.0f}s   {m['fitted_rate_hz']:9.1f} {lost / base * 100:6.2f}% "
                    f"{per_w:9.3f} {m['write_rate_actual']:6.1f}"
                )
            result["hold"] = windows
            if len(windows) > 1:
                pw = [w["lost_ticks_per_write"] for w in windows]
                _, slope, _ = _fit_line([w["t_end"] for w in windows], pw)
                result["hold_drift_ticks_per_write_per_min"] = slope * 60.0
                print(
                    f"\n  ticks/write {min(pw):.3f}..{max(pw):.3f}, "
                    f"drift {slope * 60:+.4f} per minute"
                )
                print(
                    "  A flat line means the halt cost is stationary, and so cannot "
                    "be what grows over a run."
                )
            path = d.stamped("haltshape_hold", "json")
            path.write_text(json.dumps(result, indent=2))
            print(f"\nwrote {path}")
            return 0

        print(f"\n=== sweep: payload vs lost ticks ({len(payloads)} conditions) ===")
        hdr = (
            f"{'payload':>8} {'wr/s':>7} {'KiB/s':>7} {'dR/dt':>9} "
            f"{'loss%':>7} {'ticks/wr':>9} {'cycles/wr':>10}"
        )
        print(hdr)
        print("-" * len(hdr))
        rows: list[dict] = []
        for p in payloads:
            rate = args.byte_rate / p if args.byte_rate > 0 else args.write_rate
            m = measure(
                be,
                args.secs,
                r_rate=args.r_rate,
                eff=eff,
                payload=p,
                write_rate=rate,
                addr=args.addr,
            )
            lost = base - m["fitted_rate_hz"]
            per_w = lost / m["write_rate_actual"] if m["write_rate_actual"] > 0 else 0.0
            m["lost_ticks_per_sec"] = lost
            m["lost_ticks_per_write"] = per_w
            m["halt_cycles_per_write"] = per_w * cycles_per_tick
            rows.append(m)
            print(
                f"{p:8d} {m['write_rate_actual']:7.1f} {m['byte_rate'] / 1024:7.1f} "
                f"{m['fitted_rate_hz']:9.1f} {lost / base * 100:6.2f}% "
                f"{per_w:9.3f} {per_w * cycles_per_tick:10.1f}"
            )
        result["sweep"] = rows

        xs = [float(r["payload"]) for r in rows]
        ys = [r["lost_ticks_per_write"] for r in rows]
        a, b, r2 = _fit_line(xs, ys)
        result["fit"] = {
            "fixed_ticks_per_write": a,
            "ticks_per_byte": b,
            "r2": r2,
            "fixed_cycles_per_write": a * cycles_per_tick,
            "cycles_per_byte": b * cycles_per_tick,
        }
        print(f"\n{'=' * 62}")
        print(f"[fit] lost_ticks_per_write = {a:+.4f} + {b:.6f} * payload_bytes   (r2 {r2:.3f})")
        print(f"  fixed cost   {a * cycles_per_tick:8.1f} cycles/write  ({a:.3f} NMI ticks)")
        print(
            f"  per-byte     {b * cycles_per_tick:8.3f} cycles/byte   "
            f"(the REC DMA model predicts 1.000)"
        )
        at_worker = a + b * 128
        print(
            f"  at the worker's 128 B quantum x 91 writes/s: "
            f"{at_worker:.2f} ticks/write = {at_worker * 91 / eff * 100:.2f}% of the stream"
        )
    except KeyboardInterrupt:
        print("\n[interrupted]")
    finally:
        disarm(be)
        be.silence_sid()
        be.reset()
        be.close()

    path = d.stamped("haltshape", "json")
    path.write_text(json.dumps(result, indent=2))
    print(f"\nwrote {path}")
    print("Machine silenced + reset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

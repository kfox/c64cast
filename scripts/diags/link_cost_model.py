#!/usr/bin/env python3
"""Fit what a write costs the *host link*, in wall-clock seconds, as

    cost(B)  =  max(floor, intercept + per_byte * B)

This is where ``HardwareProfile.write_cost_*`` comes from — re-run it per
backend when firmware or transport changes, and paste the three constants onto
the profile.

    scripts/diags/link_cost_model.py --url u64://192.168.2.64
    scripts/diags/link_cost_model.py --url tr://
    scripts/diags/link_cost_model.py --url u64://… --json out/cost_u64.json

``write_region`` uses that model to choose between one write covering the whole
dirty span and several covering only the dirty slabs inside it. Which is right
is entirely a property of the link, and the two backends here sit at opposite
extremes: the Ultimate charges ~5.2 ms per write and nothing for payload up to
~2.4 KB, so chunking multiplies cost; the TeensyROM charges ~0.29 ms per write
and is otherwise all payload, so chunking pays. **Both were measured with this
tool** (2026-08-12, r2 = 1.0000 per cell). The rule this replaced compared byte
counts, which is the wrong currency on a link with a floor that high.

Two regimes rather than a single line because that is what the links measure
as, and the distinction is the whole result: a straight fit through the flat
part and the sloped part reports a healthy r2 while understating the floor and
the slope at once, and the floor is the term the chunking decision turns on.

Not the same quantity as ``halt_shape_probe``, which fits the same shape from
the *C64's* side in lost NMI ticks. That one says what a write costs the 6510;
this one says what it costs the frame budget. They can disagree — a link whose
fixed cost is dominated by a TCP round trip has a large ``a`` the C64 never
sees — and it is this one that decides how a frame should be cut into writes.

**Two-stage fit, because a burst's total time carries a constant the per-write
cost is not.** ``flush()`` is a round trip (IDENTIFY on socket DMA), so timing
one burst measures ``c + N * (a + b*B)``. Fitting total against ``N`` at fixed
``B`` puts that round trip in the intercept and the per-write cost in the
slope; fitting those slopes against ``B`` then separates ``a`` from ``b``.
Timing a single write instead would fold one whole round trip into ``a`` and
inflate it by orders of magnitude.

The flush barrier is not optional on the Ultimate: ``DMAWRITE`` is
fire-and-forget (``writes_are_acked=False``), so without it the loop measures
how fast the kernel accepts bytes into the socket buffer, not how fast the
device drains them. On the TeensyROM every write is acked, so the barrier is
nearly free and the numbers are already serialized.

**Bursts shorter than the socket buffer measure the buffer, not the link.** A
20-write burst of 64 B clocks 545 writes/s here and a 320-write burst of the
same payload clocks 167 — the first one fits entirely in the kernel's send
buffer and returns before the device has drained any of it. Fitting across that
knee yields a negative intercept and a meaningless slope, so ``--bursts``
defaults to lengths past it. Anything under ~80 writes is buffer-depth
telemetry and does not belong in the fit.

Writes land on scratch RAM with nothing running on the C64 — this measures the
cost of a write, not playback. Payload entropy is irrelevant; neither link
compresses the segment body.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

import _diaglib as d

from c64cast.app.config import Config
from c64cast.app.connect import apply_to_config, parse_connection_uri
from c64cast.hw.backend import make_backend

SCRATCH_ADDR = 0x6000  # free BASIC RAM; clear of the audio ring ($4000-$5FFF)
# Spans the range write_region actually chooses between, up to the 8 KB bitmap
# region that is the largest single push in the tree.
DEFAULT_PAYLOADS = (8, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192)
DEFAULT_BURSTS = (80, 160, 320)  # past the socket buffer — see the module docstring


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


class FlushFailed(RuntimeError):
    """The flush barrier did not complete, so the burst time means nothing."""


def strict_flush(be) -> None:
    """Flush, raising if the barrier failed.

    ``Ultimate64API.flush`` swallows transport errors into a log warning — the
    right call for a playlist, fatal for a measurement: a failed barrier returns
    immediately and the burst it was bracketing clocks an impossible rate (a run
    of this probe reported 28,937 writes/s that way, and the corrupted cells were
    otherwise indistinguishable from real ones). Reach past it to the client,
    whose flush does raise.
    """
    client = getattr(be, "socket_dma", None)
    try:
        if client is not None:
            client.flush()
        else:
            be.flush()  # TeensyROM: acked writes, no swallowing wrapper
    except Exception as e:  # noqa: BLE001 — any transport failure invalidates the cell
        raise FlushFailed(f"{type(e).__name__}: {e}") from e


def widen_io_timeout(be, secs: float) -> None:
    """Raise the DMA socket's read timeout for the duration of the probe.

    The default is 2 s, sized for the app, which never queues more than a
    frame. This tool deliberately queues whole bursts: 320 writes at ~5 ms is
    1.7 s of drain that the trailing IDENTIFY must wait out, so the barrier
    times out on its way to a perfectly good answer. Closing after the
    assignment forces the next send to reconnect and apply it.
    """
    client = getattr(be, "socket_dma", None)
    if client is None:
        return
    client.io_timeout = secs
    client.close()


def fit_piecewise(xs: list[float], ys: list[float]) -> tuple[float, float, float, float, float]:
    """Fit ``cost(B) = max(floor, intercept + slope*B)``.

    Returns ``(floor, intercept, slope, knee_bytes, sse)``.

    A single straight line is the wrong shape and flatters itself: the measured
    curve is flat from 8 B to ~2 KB and linear above, so least-squares through
    all of it reports a healthy r2 while getting both terms wrong (it splits the
    difference, understating the floor and the slope at once). The floor is real
    — per-command overhead the payload never touches — and it is the term the
    chunking decision turns on, so it has to be fit separately.

    The split point is chosen by brute force over the candidate indices rather
    than assumed, so the knee is a result rather than an input.
    """
    best: tuple[float, float, float, float, float] | None = None
    for split in range(1, len(xs) - 1):
        lo_y = ys[:split]
        hi_x, hi_y = xs[split:], ys[split:]
        if len(hi_x) < 2:
            continue
        floor = sum(lo_y) / len(lo_y)
        intercept, slope, _ = _fit_line(hi_x, hi_y)
        if slope <= 0:
            continue
        sse = sum((y - floor) ** 2 for y in lo_y)
        sse += sum((y - (intercept + slope * x)) ** 2 for x, y in zip(hi_x, hi_y, strict=True))
        if best is None or sse < best[4]:
            knee = (floor - intercept) / slope
            best = (floor, intercept, slope, knee, sse)
    if best is None:  # no upward regime in range — the link is purely count-bound
        floor = sum(ys) / len(ys)
        return floor, floor, 0.0, float("inf"), 0.0
    return best


def time_burst(be, *, n: int, payload: bytes, addr: int) -> float:
    """Wall-clock seconds for ``n`` writes of ``payload``, flush-to-flush."""
    tag = f"{addr:04X}"
    strict_flush(be)  # drain anything outstanding so it isn't billed to this burst
    t0 = time.perf_counter()
    for _ in range(n):
        be.write_memory_file(tag, payload)
    strict_flush(be)
    return time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help="connection target (u64://…, tr://…)")
    ap.add_argument(
        "--payloads",
        default=",".join(str(p) for p in DEFAULT_PAYLOADS),
        help="comma-separated payload sizes (bytes) to sweep",
    )
    ap.add_argument(
        "--bursts",
        default=",".join(str(b) for b in DEFAULT_BURSTS),
        help="comma-separated burst lengths (writes) per payload",
    )
    ap.add_argument("--reps", type=int, default=3, help="repeats per (payload, burst) cell")
    ap.add_argument("--settle", type=float, default=0.25, help="idle seconds between bursts")
    ap.add_argument("--addr", type=lambda s: int(s, 16), default=SCRATCH_ADDR)
    ap.add_argument(
        "--io-timeout",
        type=float,
        default=30.0,
        help="DMA socket read timeout (s) — must exceed a burst's drain time",
    )
    ap.add_argument("--json", default="", help="write the full result set here")
    ap.add_argument(
        "--no-reset", action="store_true", help="skip the end-of-run reset (leave the machine up)"
    )
    args = ap.parse_args()

    payloads = [int(p) for p in args.payloads.split(",") if p.strip()]
    bursts = [int(b) for b in args.bursts.split(",") if b.strip()]

    print(f"[setup] {args.url}  scratch ${args.addr:04X}")
    print(
        f"[setup] payloads {payloads}  bursts {bursts}  reps {args.reps}  "
        f"({len(payloads) * len(bursts) * args.reps} bursts total)"
    )

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)

    result: dict = {"url": args.url, "payloads": payloads, "bursts": bursts, "cells": []}
    per_write: dict[int, float] = {}
    flush_cost: dict[int, float] = {}

    try:
        widen_io_timeout(be, args.io_timeout)
        # Warm the link: first write pays connect + handshake, which belongs to
        # neither term of the fit.
        for _ in range(5):
            be.write_memory_file(f"{args.addr:04X}", bytes(64))
        strict_flush(be)

        hdr = f"{'payload':>8} {'writes':>7} {'total_ms':>9} {'per_wr_ms':>10} {'wr/s':>7}"
        print(f"\n{hdr}\n{'-' * len(hdr)}")

        for b_size in payloads:
            payload = bytes([0x5A]) * b_size
            ns: list[float] = []
            totals: list[float] = []
            for n in bursts:
                samples = []
                failures = 0
                while len(samples) < args.reps and failures < args.reps:
                    time.sleep(args.settle)
                    try:
                        samples.append(time_burst(be, n=n, payload=payload, addr=args.addr))
                    except FlushFailed as e:
                        failures += 1
                        print(f"{b_size:>8} {n:>7}   [flush failed: {e}] retrying")
                        time.sleep(1.0)
                if not samples:
                    print(f"{b_size:>8} {n:>7}   [no valid sample — cell dropped]")
                    continue
                total = statistics.median(samples)
                ns.append(float(n))
                totals.append(total)
                print(
                    f"{b_size:>8} {n:>7} {total * 1000:>9.1f} "
                    f"{total / n * 1000:>10.3f} {n / total:>7.1f}"
                )
                result["cells"].append(
                    {
                        "payload": b_size,
                        "writes": n,
                        "total_s": total,
                        "samples_s": samples,
                    }
                )
            c, slope, r2 = _fit_line(ns, totals)
            per_write[b_size] = slope
            flush_cost[b_size] = c
            print(
                f"  fit: per-write {slope * 1000:.3f} ms, flush overhead "
                f"{c * 1000:.2f} ms, r2 {r2:.4f}"
            )

        # Stage two: per-write cost against payload size.
        xs = [float(p) for p in sorted(per_write)]
        ys = [per_write[int(x)] for x in xs]
        floor, intercept, slope, knee, _ = fit_piecewise(xs, ys)

        def cost(nbytes: float) -> float:
            return max(floor, intercept + slope * nbytes)

        print("\n=== cost model ===")
        print("  cost(B) = max(floor, intercept + slope*B)")
        print(
            f"    floor      {floor * 1000:>10.3f} ms   (per-write overhead; payload is free below the knee)"
        )
        print(f"    intercept  {intercept * 1000:>10.3f} ms")
        print(f"    slope      {slope * 1e6:>10.4f} us/byte")
        print(f"    knee       {knee:>10.0f} bytes")

        print("\n=== what this means for write_region ===")
        print(
            f"  A payload under ~{knee:.0f} B costs the same as an 8 B one, so splitting any\n"
            f"  region smaller than the knee into k chunks costs exactly k times as much\n"
            f"  as pushing the whole thing in one write."
        )
        hdr = f"  {'region':>18} {'full push':>10} {'max chunks':>11}"
        print(f"\n{hdr}\n  {'-' * (len(hdr) - 2)}")
        regions = [("screen ($0400)", 1000), ("color ($D800)", 1000), ("bitmap ($2000)", 8000)]
        budget = {}
        for label, nbytes in regions:
            full = cost(nbytes)
            k_max = full / floor
            budget[label] = k_max
            print(f"  {label:>18} {full * 1000:>9.2f}ms {k_max:>10.1f}")
        print(
            "\n  'max chunks' is the break-even: chunking into more separate writes than\n"
            "  that is slower than simply pushing the entire region."
        )

        result["fit"] = {
            "floor_s": floor,
            "intercept_s": intercept,
            "slope_s_per_byte": slope,
            "knee_bytes": knee,
        }
        result["chunk_budget"] = budget
        result["per_write_s"] = {str(k): v for k, v in per_write.items()}
        result["flush_s"] = {str(k): v for k, v in flush_cost.items()}

        if args.json:
            path = d.out_dir() / args.json if "/" not in args.json else args.json
            with open(path, "w") as f:
                json.dump(result, f, indent=2)
            print(f"\n[json] {path}")
    finally:
        close = getattr(be, "close", None)
        if close:
            close()
        if not args.no_reset:
            ok = d.machine_reset(args.url)
            print(f"[reset] {args.url}: {'ok' if ok else 'FAILED'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""What a TeensyROM+ write costs the *host*: WriteC64Mem against WriteC64Spans.

    scripts/diags/tr_spans_cost.py --url tr://
    scripts/diags/tr_spans_cost.py --url tr:// --slices 0,16,32,64 --gaps 20,40

Slicing is the firmware's answer to the halt a large write puts on the 6510 —
the side ``halt_shape_probe.py`` and ``audio_fm_probe.py`` measure. This is the
other side of that trade: each slice adds a DMA handshake and ``gap`` µs of
idle bus, so a sliced write takes longer to ack, and on a link that is already
bus-DMA-bound for bitmap video that is frame rate. The tool measures the
round trip of one command, ack included, which is what the render thread
waits on:

  * ``mem``      — one WriteC64Mem per payload (the pre-spans path).
  * ``spans s/g`` — one WriteC64Spans carrying the payload as one span, sliced
    at ``s`` bytes with a ``g`` µs gap (``s`` = 0 is a single halt).
  * ``batch``    — ``k`` separate 64-byte spans, the shape write_region's
    chunked delta produces, as k WriteC64Mem against one WriteC64Spans.

The C64 runs the BASIC clear loop throughout, as it does under a show: the
firmware starts a slice promptly only while the 6510 is writing memory, so a
CPU parked in a read-only loop would measure its badline wait instead.

Writes land at $6000 (plain RAM, clear of the audio ring and the NMI handler).
Resets the machine on exit.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time

import _diaglib as d

from c64cast.app.config import Config
from c64cast.app.connect import apply_to_config, parse_connection_uri
from c64cast.hw.backend import make_backend
from c64cast.hw.teensyrom_api import TeensyROMBackend
from c64cast.hw.teensyrom_dma import SPANS_FIELD_MAX, SPANS_MAX, SPANS_MAX_SLICES, TRClient

SCRATCH = 0x6000
BATCH_SPAN_BYTES = 64
BATCH_STRIDE = 128  # spans separated by clean gaps, as dirty delta slabs are


def _time(fn, reps: int) -> dict:
    samples = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    samples.sort()
    return {
        "median_ms": statistics.median(samples) * 1e3,
        "p95_ms": samples[min(len(samples) - 1, int(0.95 * len(samples)))] * 1e3,
        "max_ms": samples[-1] * 1e3,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="tr://")
    ap.add_argument("--payloads", default="64,256,1000,2048,4096")
    ap.add_argument("--slices", default="0,8,16,32,64,128")
    ap.add_argument("--gaps", default="40")
    ap.add_argument("--batch", default="4,8,16", help="span counts for the batch rows")
    ap.add_argument("--reps", type=int, default=40)
    args = ap.parse_args()

    payloads = [int(x) for x in args.payloads.split(",")]
    slices = [int(x) for x in args.slices.split(",")]
    gaps = [int(x) for x in args.gaps.split(",")]
    batches = [int(x) for x in args.batch.split(",") if x]
    # Out of range, write_spans/write_segment raise only after the reset and
    # clear loop, mid-grid — check here instead.
    if args.reps < 1:
        ap.error("--reps must be at least 1")
    if not all(1 <= n <= TRClient.MAX_SEGMENT_BYTES for n in payloads):
        ap.error(f"--payloads must each be 1-{TRClient.MAX_SEGMENT_BYTES}")
    if not all(0 <= v <= SPANS_FIELD_MAX for v in slices + gaps):
        ap.error(f"--slices and --gaps must each be 0-{SPANS_FIELD_MAX}")
    if not all(1 <= k <= SPANS_MAX for k in batches):
        ap.error(f"--batch counts must each be 1-{SPANS_MAX}")

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    cfg.teensyrom.dma_slicing = "off"  # this tool drives both commands itself
    be = make_backend(cfg)
    if not isinstance(be, TeensyROMBackend):
        print("[abort] not a TeensyROM URL")
        be.close()
        return 1
    tr = be.tr
    rows: list[dict] = []
    try:
        if not tr.probe_spans():
            print("[abort] firmware lacks WriteC64Spans")
            return 1
        be.reset()
        be.run_basic_clear_loop()
        print(f"[setup] {args.url}  {args.reps} reps/condition, C64 in the BASIC clear loop")
        hdr = f"{'condition':>22} {'bytes':>6} {'median':>8} {'p95':>8} {'max':>8} {'KiB/s':>8}"
        print(hdr)
        print("-" * len(hdr))

        def report(cond: str, nbytes: int, m: dict) -> None:
            kibs = nbytes / 1024 / (m["median_ms"] / 1e3)
            rows.append({"condition": cond, "bytes": nbytes, **m, "kib_s": kibs})
            print(
                f"{cond:>22} {nbytes:>6} {m['median_ms']:>8.2f} {m['p95_ms']:>8.2f} "
                f"{m['max_ms']:>8.2f} {kibs:>8.1f}"
            )

        for n in payloads:
            data = bytes([0x5A]) * n
            report("mem", n, _time(lambda data=data: tr.write_segment(SCRATCH, data), args.reps))
            for g in gaps:
                for s in slices:
                    if s and (n + s - 1) // s > SPANS_MAX_SLICES:
                        continue
                    m = _time(
                        lambda data=data, s=s, g=g: tr.write_spans([(SCRATCH, data)], s, g),
                        args.reps,
                    )
                    report(f"spans {s or 'whole'}/{g}us", n, m)

        chunk = bytes([0xA5]) * BATCH_SPAN_BYTES
        for k in batches:
            spans = [(SCRATCH + i * BATCH_STRIDE, chunk) for i in range(k)]
            nbytes = k * BATCH_SPAN_BYTES

            def as_mem(spans=spans) -> None:
                for addr, data in spans:
                    tr.write_segment(addr, data)

            report(f"batch {k} x mem", nbytes, _time(as_mem, args.reps))
            for g in gaps:
                for s in (0, 32):
                    m = _time(lambda spans=spans, s=s, g=g: tr.write_spans(spans, s, g), args.reps)
                    report(f"batch {k} spans {s or 'whole'}/{g}", nbytes, m)
    finally:
        # Whatever was measured before a failure part way through the grid
        # (a held bus, a link timeout) is kept — written before the teardown,
        # so an interrupt or error there cannot lose it.
        try:
            if rows:
                path = d.stamped("tr_spans_cost", "json")
                path.write_text(
                    json.dumps({"url": args.url, "reps": args.reps, "rows": rows}, indent=2)
                )
                print(f"\nwrote {path}")
        finally:
            be.silence_sid()
            be.reset()
            be.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Measure how much of what the host *believes* it has written is actually in
C64 RAM, and whether it arrived intact.

    scripts/diags/write_delivery_lag.py --url tr://
    scripts/diags/write_delivery_lag.py --url u64://192.168.2.64 -t 30

Every host-side audio metric the tree has (late slots, servo gap, R rate) is
computed from when a write *call returned*, which assumes a returned write has
landed. On a transport that buffers — USB CDC hands bytes to a kernel queue and
returns regardless of what the device consumed — that assumption fails silently
and every derived number looks perfect while the C64 sees something else. The
worker's ring write is fire-and-forget with no readback anywhere, so nothing in
the app can currently tell the difference.

This writes the audio worker's own pattern (128-byte quanta into the 8 KB ring
at ~90/s, strict absolute pacing) with a per-quantum marker, then reads back a
small window at the write head and reports three things:

  deficit   quanta issued but not yet present  (the buffering the servo can't see)
  read lag  how long the readback itself took  (queue depth, on a shared link)
  torn      slots holding mixed marker bytes   (a write that landed in pieces)

Deficit and torn slots are comparable across backends. Read lag is NOT: the
Ultimate reads over REST (an independent channel from the port-64 write socket)
while TeensyROM reads over the same serial link it writes on, so only the TR's
number carries queue information.

No NMI consumer is armed — this measures delivery, not playback, so the ring is
just scratch RAM here. A preflight checks the region is actually quiescent
first: on TeensyROM a reset boots into the cartridge menu, which is a live
program that could be using this RAM and would otherwise read as corruption.
"""

from __future__ import annotations

import argparse
import sys
import time

import _diaglib as d

RING_ADDR = 0x4000  # audio.py RING_BUFFER_ADDR — the region the worker uses
RING_SIZE = 0x2000  # audio.py RING_BUFFER_SIZE
DAC_RATE_HZ = 12032.1  # audio.py effective NTSC rate, for ms-of-audio conversion


def _slot_addr(slot: int, quantum: int) -> int:
    return RING_ADDR + slot * quantum


def _read_window(api, start_slot: int, count: int, quantum: int, nslots: int) -> bytes | None:
    """Read `count` consecutive slots starting at `start_slot`, wrapping the
    ring. Split into two calls across the wrap rather than reading the whole
    ring: an 8 KB read is ~0.25 s on the TR link and would stall the write
    cadence we are trying to characterise."""
    if start_slot + count <= nslots:
        return api.read_memory(_slot_addr(start_slot, quantum), count * quantum)
    first = nslots - start_slot
    a = api.read_memory(_slot_addr(start_slot, quantum), first * quantum)
    b = api.read_memory(RING_ADDR, (count - first) * quantum)
    if a is None or b is None:
        return None
    return a + b


def _preflight(api, quantum: int) -> bool:
    """Confirm nothing else on the C64 is writing this region."""
    probe = bytes([0xA5]) * quantum
    api.write_memory_file(f"{RING_ADDR:04X}", probe)
    time.sleep(1.0)
    got = api.read_memory(RING_ADDR, quantum)
    if got is None:
        print("[preflight] read_memory returned None — backend cannot read; aborting")
        return False
    if got != probe:
        differing = sum(1 for x, y in zip(got, probe, strict=True) if x != y)
        print(f"[preflight] region not quiescent: {differing}/{quantum} bytes changed in 1 s")
        print("[preflight] something on the C64 is using $4000 — pick another --addr or reset")
        return False
    print(f"[preflight] ${RING_ADDR:04X} quiescent, readback exact")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help="connection target (u64://…, tr://…)")
    ap.add_argument("-t", "--seconds", type=float, default=20.0)
    ap.add_argument("--rate", type=float, default=90.0, help="writes/sec (worker measures ~91)")
    ap.add_argument("--quantum", type=int, default=128, help="bytes per write (worker ships 128)")
    ap.add_argument("--window", type=int, default=8, help="slots to read back at the head")
    ap.add_argument("--sample-every", type=float, default=1.0, help="seconds between readbacks")
    ap.add_argument(
        "--load-kib",
        type=float,
        default=0.0,
        help="interleave background writes to $6000 at this KiB/s, standing in for the "
        "video path's share of the link (a real petscii run measures ~34 KiB/s and "
        "~114 writes/s total, of which audio is ~11.5 KiB/s at 91 writes/s)",
    )
    ap.add_argument("--load-quantum", type=int, default=1024, help="bytes per background write")
    ap.add_argument("--no-reset", action="store_true", help="skip the end-of-run machine reset")
    args = ap.parse_args()

    from c64cast.backend import make_backend
    from c64cast.config import Config
    from c64cast.connect import apply_to_config, parse_connection_uri

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    api = make_backend(cfg)

    nslots = RING_SIZE // args.quantum
    quantum, window = args.quantum, min(args.window, nslots)
    print(f"[setup] {args.url}  quantum={quantum}B  slots={nslots}  rate={args.rate:g}/s")

    if not _preflight(api, quantum):
        return 1

    period = 1.0 / args.rate
    load_payload = bytes(args.load_quantum)
    load_period = (
        args.load_quantum / (args.load_kib * 1024.0) if args.load_kib > 0 else float("inf")
    )
    if args.load_kib > 0:
        print(
            f"[load] +{args.load_kib:g} KiB/s as {args.load_quantum}B writes "
            f"({1.0 / load_period:.0f}/s) to $6000"
        )

    t0 = time.monotonic()
    deadline = t0
    load_deadline = t0 + load_period
    next_sample = t0 + args.sample_every
    g = 0  # global quantum index; marker = g & 0xFF, unique over 4 ring laps
    load_writes = 0
    samples: list[tuple[float, int, float, int]] = []

    print(f"\n{'t':>6} {'deficit':>8} {'bytes':>7} {'ms audio':>9} {'read lag':>9} {'torn':>5}")
    try:
        while time.monotonic() - t0 < args.seconds:
            now = time.monotonic()
            if now < deadline:
                time.sleep(deadline - now)
            deadline += period

            slot = g % nslots
            api.write_memory_file(f"{_slot_addr(slot, quantum):04X}", bytes([g & 0xFF]) * quantum)
            g += 1

            # Interleaved rather than on its own thread: the app's video and
            # audio writes share one backend and one link, so a competing
            # thread would test a contention path the real run doesn't have.
            if time.monotonic() >= load_deadline:
                api.write_memory_file("6000", load_payload)
                load_deadline += load_period
                load_writes += 1

            if time.monotonic() < next_sample:
                continue
            next_sample += args.sample_every

            # Window ends at the slot just written, so deficit counts backwards
            # from the head: the newest slot not yet present is the frontier.
            issued = g - 1
            start = (issued - window + 1) % nslots
            r0 = time.monotonic()
            buf = _read_window(api, start, window, quantum, nslots)
            read_lag = time.monotonic() - r0
            if buf is None:
                print(f"{time.monotonic() - t0:6.1f}   read failed")
                continue

            deficit, torn = 0, 0
            for k in range(window):
                idx = issued - window + 1 + k
                piece = buf[k * quantum : (k + 1) * quantum]
                if len(set(piece)) > 1:
                    torn += 1
                elif piece[0] != (idx & 0xFF):
                    deficit += 1
            t = time.monotonic() - t0
            samples.append((t, deficit, read_lag, torn))
            print(
                f"{t:6.1f} {deficit:8d} {deficit * quantum:7d} "
                f"{deficit * quantum / DAC_RATE_HZ * 1000:9.1f} "
                f"{read_lag * 1000:8.1f}ms {torn:5d}"
            )
    except KeyboardInterrupt:
        print("\n[interrupted]")
    finally:
        if samples:
            defs = [s[1] for s in samples]
            lags = [s[2] for s in samples]
            torns = sum(s[3] for s in samples)
            print(
                f"\n{'=' * 60}\n[summary] {args.url}  {len(samples)} samples over {args.seconds:g}s"
            )
            print(
                f"  deficit  mean {sum(defs) / len(defs):.2f} quanta  max {max(defs)}  "
                f"({max(defs) * quantum / DAC_RATE_HZ * 1000:.1f} ms of audio worst case)"
            )
            print(
                f"  read lag mean {sum(lags) / len(lags) * 1000:.1f} ms  "
                f"max {max(lags) * 1000:.1f} ms"
            )
            print(f"  torn slots: {torns}")
            elapsed = time.monotonic() - t0
            kib = (g * quantum + load_writes * args.load_quantum) / 1024.0 / elapsed
            print(
                f"  writes issued: {g} ring + {load_writes} load "
                f"= {(g + load_writes) / elapsed:.0f}/s, {kib:.1f} KiB/s"
            )
        if not args.no_reset:
            ok = d.machine_reset(args.url)
            print(f"[reset] {args.url}: {'OK' if ok else 'FAILED — RESET THE MACHINE BY HAND'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
